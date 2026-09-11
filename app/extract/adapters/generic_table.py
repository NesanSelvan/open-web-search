"""Table-panel adapter — the workhorse for retailer PDPs.

Swiggy, Zepto, BigBasket and Nutrabay all print the panel as an HTML table or a
label/value list. One parser handles all of them; the per-domain subclasses only
declare which domain they serve and what tier the source earns.

Basis detection is the part that matters. We READ the basis off the column header
and never assume per-100g — the existing pipeline has been bitten by per-serve
columns silently treated as per-100g. When BOTH columns are printed we take the
per-100g column verbatim and do no arithmetic at all.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from app.domain.models import Basis, Panel, Tier
from app.extract.adapters.base import map_label
from app.extract.panel import (
    build_panel,
    is_combo,
    is_viable,
    parse_number,
    should_drop_label,
    to_grams,
)
from app.scrape.policy import registrable_domain

_PER100G_RE = re.compile(r"per\s*100\s*g|/\s*100\s*g|100\s*g\b", re.I)
_PER100ML_RE = re.compile(r"per\s*100\s*ml|/\s*100\s*ml|100\s*ml\b", re.I)
_SERVING_RE = re.compile(r"per\s*serv|serving|per\s*scoop|scoop", re.I)
_PACK_RE = re.compile(r"per\s*pack|whole\s*pack", re.I)
_SERVING_G_RE = re.compile(r"serving\s*size[^0-9]{0,20}(\d+(?:\.\d+)?)\s*(g|ml)", re.I)
_NUTRI_HINT_RE = re.compile(r"nutrition|nutritional\s+information|nutrition\s+facts", re.I)


def _basis_from(text: str) -> Basis | None:
    if _PER100G_RE.search(text):
        return Basis.PER_100G
    if _PER100ML_RE.search(text):
        return Basis.PER_100ML
    if _PACK_RE.search(text):
        return Basis.PER_PACK
    if _SERVING_RE.search(text):
        return Basis.PER_SERVING
    return None


def _cells(row) -> list[str]:
    return [c.text(strip=True) for c in row.css("td, th")]


class TablePanelAdapter:
    """Base class. Subclasses set `domains`, `source_type` and `tier`."""

    domains: tuple[str, ...] = ()
    source_type: str = "retailer_label"
    tier: Tier = Tier.C

    def can_parse(self, html: str, url: str) -> bool:
        return bool(_NUTRI_HINT_RE.search(html))

    def parse(self, html: str, markdown: str, url: str) -> Panel | None:
        tree = HTMLParser(html)

        product = self._product_name(tree)
        if not product:
            return None
        if is_combo(product):
            return None      # caller turns a None here into rejected:combo_pack

        values, basis, serving_g = self._read_tables(tree)
        if not values:
            values, basis2, serving_g2 = self._read_markdown(markdown)
            basis = basis or basis2
            serving_g = serving_g or serving_g2
        if not values or not is_viable(values):
            # Better no answer than a number scraped off page furniture.
            return None

        if serving_g is None:
            match = _SERVING_G_RE.search(html)
            if match:
                serving_g = float(match.group(1))

        return build_panel(
            product=product,
            values=values,
            basis=basis or Basis.PER_100G,
            serving_g=serving_g,
            url=url,
            domain=registrable_domain(url),
            source_type=self.source_type,
            tier=self.tier,
            extractor=f"adapter:{registrable_domain(url)}",
            brand=self._brand(tree),
            notes=[] if basis else ["basis not printed — assumed per-100g"],
        )

    # ------------------------------------------------------------------ parts
    def _product_name(self, tree: HTMLParser) -> str | None:
        for sel in ('meta[property="og:title"]', 'meta[name="title"]'):
            node = tree.css_first(sel)
            if node and node.attributes.get("content"):
                return node.attributes["content"].strip()
        h1 = tree.css_first("h1")
        return h1.text(strip=True) if h1 else None

    def _brand(self, tree: HTMLParser) -> str | None:
        node = tree.css_first('meta[property="product:brand"], meta[itemprop="brand"]')
        return node.attributes.get("content", "").strip() or None if node else None

    def _read_tables(self, tree: HTMLParser):
        best: dict[str, float | None] = {}
        best_basis: Basis | None = None
        serving_g: float | None = None

        for table in tree.css("table"):
            rows = table.css("tr")
            if not rows:
                continue

            header = " ".join(_cells(rows[0]))
            # Which column is per-100g? When both bases are printed we want that
            # one verbatim rather than scaling the per-serve column.
            col_basis: list[Basis | None] = [_basis_from(c) for c in _cells(rows[0])]
            table_basis = _basis_from(header)

            preferred_col = None
            for idx, cb in enumerate(col_basis):
                if cb in (Basis.PER_100G, Basis.PER_100ML):
                    preferred_col = idx
                    table_basis = cb
                    break

            values: dict[str, float | None] = {}
            for row in rows:
                cells = _cells(row)
                if len(cells) < 2:
                    continue
                label = cells[0]
                if should_drop_label(label):
                    continue
                key = map_label(label)
                if not key:
                    continue

                if preferred_col is not None and preferred_col < len(cells):
                    raw = cells[preferred_col]
                else:
                    raw = next((c for c in cells[1:] if parse_number(c) is not None), "")
                num = parse_number(raw)
                if num is None:
                    continue

                # Composition tables split the unit into its own column
                # (`NUTRIENT | Amount | Unit`) and list energy TWICE — once in kJ,
                # once in kcal. Taking whichever came last is luck, not a rule.
                unit = " ".join(cells[1:]).lower()
                if key == "cal" and re.search(r"\bkj\b", unit) and not re.search(r"\bkcal\b", unit):
                    continue

                # `NUTRIENT | Amount | Unit` puts the unit in its own cell.
                unit_cell = cells[2] if len(cells) > 2 else ""
                values.setdefault(key, to_grams(key, num, unit_cell))

            if len(values) > len(best):
                best, best_basis = values, table_basis

        return best, best_basis, serving_g

    def _read_markdown(self, markdown: str):
        """Markdown pipe-tables, for pages whose panel survives only in the text."""
        values: dict[str, float | None] = {}
        basis = _basis_from(markdown[:4000]) if markdown else None
        for line in (markdown or "").splitlines():
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) < 2 or should_drop_label(parts[0]):
                continue
            key = map_label(parts[0])
            if not key:
                continue
            num = next((parse_number(p) for p in parts[1:] if parse_number(p) is not None), None)
            if num is not None:
                # `NUTRIENT | Amount | Unit` puts the unit in its own cell.
                unit_cell = cells[2] if len(cells) > 2 else ""
                values.setdefault(key, to_grams(key, num, unit_cell))
        return values, basis, None


class GenericTableAdapter(TablePanelAdapter):
    """Unknown-domain fallback.

    Registered for NO domain, and tried last. This exists because the reader used
    to run a table parser ONLY for domains someone had hardcoded — so a page like
    anuvaad.org.in, carrying a clean `NUTRIENT | Amount | Unit` composition table,
    was skipped entirely and reported as `no_panel`. The long tail of nutrition
    sites is most of the web; refusing to read a printed table just because we have
    not met the domain before is throwing away the easy half of the corpus.

    Provenance is unverified, so it stays tier d and says so — it can never
    outrank a retailer's transcribed label.
    """

    domains = ()
    source_type = "generic_table"
    tier = Tier.D


class SwiggyAdapter(TablePanelAdapter):
    domains = ("swiggy.com",)
    source_type = "swiggy_instamart_seo"
    tier = Tier.C


class ZeptoAdapter(TablePanelAdapter):
    domains = ("zeptonow.com",)
    source_type = "zepto"
    tier = Tier.C


class NutrabayAdapter(TablePanelAdapter):
    domains = ("nutrabay.com",)
    source_type = "nutrabay"
    tier = Tier.B     # prints BOTH per-serve and per-100g columns — best text panel

