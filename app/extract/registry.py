"""Reader selection: known-domain adapter -> structured data. That is all.

There is deliberately NO model in this path. Both readers take numbers that are
literally printed on the page, so neither can invent one. The hosted extractor this
replaces was LLM-mediated and did invent them — a whole 120/24/3/1.5 panel for
BeastLife, Blinkit protein lifted out of og:description, Amazon protein lifted out
of the product title. A guessed nutrition value is worse than no value, because
nothing downstream can tell the difference.

A domain with no adapter therefore returns no_panel and is logged as a miss. The
miss list is the backlog: it names exactly which adapter to write next.
"""

from __future__ import annotations

import logging

from app.domain.models import Page, Panel, RejectReason, Rejection, Tier
from app.extract.adapters import (
    BigBasketAdapter,
    GenericTableAdapter,
    FatSecretAdapter,
    NutrabayAdapter,
    ShopifyJsonLdAdapter,
    SwiggyAdapter,
    ZeptoAdapter,
)
from app.extract.clean import to_markdown
from app.scrape.policy import DomainPolicy, registrable_domain
from app.settings import Settings

log = logging.getLogger(__name__)

_DOMAIN_ADAPTERS = [
    SwiggyAdapter(),
    ZeptoAdapter(),
    BigBasketAdapter(),
    NutrabayAdapter(),
    FatSecretAdapter(),
]
_GENERIC = GenericTableAdapter()


class Reader:
    def __init__(self, settings: Settings, policies=None):
        self._policies = policies
        # The structured reader needs the policy book to know how much a domain's
        # numbers are worth. Without it every site is "unknown" -> tier d.
        self._structured = ShopifyJsonLdAdapter(policies)
        self._by_domain = {
            domain: adapter
            for adapter in _DOMAIN_ADAPTERS
            for domain in adapter.domains
        }

    def _apply_trust(self, panel: Panel, policy: DomainPolicy) -> Panel:
        """An explicit `trust:` in domains.yaml overrides an adapter's default tier.

        Tier is a judgement about a SOURCE, and the deployment's config is where
        that judgement belongs — an adapter only knows how it parsed the page, not
        whether anyone vouches for the site. Without this, a laboratory composition
        table read by the generic reader stayed tier d while a hardcoded adapter
        claimed tier c for a shop.
        """
        if policy.trust and policy.trust != "unknown":
            panel.tier = Tier(policy.tier)
            panel.notes.append(f"tier from domain trust: {policy.trust}")
        return panel

    async def read(self, page: Page, policy: DomainPolicy) -> Panel | Rejection:
        url = page.final_url or page.url
        domain = registrable_domain(url)

        if policy.image_only_panel:
            # The numbers exist, but only inside a pack photo. Guessing from the
            # title or the description is exactly how the old extractor fabricated
            # panels — so we stop here and say why.
            return Rejection(RejectReason.IMAGE_ONLY, url, f"{domain} prints its panel as an image")

        markdown = to_markdown(page.html, url)

        adapter = self._by_domain.get(domain)
        if adapter and adapter.can_parse(page.html, url):
            panel = adapter.parse(page.html, markdown, url)
            if panel:
                panel.fetched_at = page.fetched_at
                return self._apply_trust(panel, policy)
            log.info("adapter for %s found no panel, falling through", domain)

        if self._structured.can_parse(page.html, url):
            panel = self._structured.parse(page.html, markdown, url)
            if panel:
                panel.fetched_at = page.fetched_at
                return self._apply_trust(panel, policy)

        # Last resort: the page may simply print a nutrition table we can read
        # even though we have never seen the domain. Still deterministic — the
        # numbers come off the page, not out of a model.
        if _GENERIC.can_parse(page.html, url):
            panel = _GENERIC.parse(page.html, markdown, url)
            if panel:
                panel.fetched_at = page.fetched_at
                panel.notes.append(f"generic table reader on {domain}")
                return self._apply_trust(panel, policy)

        return Rejection(
            RejectReason.NO_PANEL,
            url,
            f"no readable nutrition table on {domain}",
        )
