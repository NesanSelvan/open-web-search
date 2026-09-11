"""Per-domain scrape policy, loaded from config/domains.yaml.

The correction this encodes: Google's 30s pacing is a *Google* tax, not a global
rate. Swiggy, Zepto, BigBasket and the rest each get their own speed limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml


# How much a domain's numbers are worth, independent of how convenient they were
# to parse. Kept as config because it is a JUDGEMENT about a source, not a fact
# about the web — a different deployment will trust different sites.
TRUST_TIERS = {
    "lab": "a",         # official composition table / laboratory analysis
    "brand": "b",       # the manufacturer transcribing its own pack label
    "retailer": "c",    # a shop transcribing a label it stocks
    "aggregator": "d",  # crowd or API aggregate
    "unknown": "d",
}


@dataclass(frozen=True, slots=True)
class DomainPolicy:
    domain: str
    rate_per_min: float
    needs_browser: bool
    needs_residential: bool
    cache_ttl_s: int
    respect_robots: bool
    geo: tuple[float, float] | None = None
    image_only_panel: bool = False
    # Lower sorts first when choosing which results to open. None = unranked.
    rank: int | None = None
    trust: str = "unknown"

    @property
    def tier(self) -> str:
        return TRUST_TIERS.get(self.trust, "d")


def registrable_domain(url_or_host: str) -> str:
    """`https://www.swiggy.com/instamart/p/x` -> `swiggy.com`.

    Deliberately simple: strip scheme/path, drop a leading `www.`, then keep the
    last two labels — or three when the middle one is a known second-level suffix
    (co.in, com.au, co.uk...), which is exactly the fatsecret.co.in case.
    """
    host = url_or_host
    if "//" in host:
        host = urlsplit(host).hostname or ""
    host = host.lower().strip().removeprefix("www.")
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in {"co", "com", "net", "org", "gov", "ac"} and len(labels[-1]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class PolicyBook:
    def __init__(self, defaults: dict, domains: dict, image_only: set[str]):
        self._defaults = defaults
        self._domains = domains
        self._image_only = image_only

    @classmethod
    def load(cls, path: Path) -> "PolicyBook":
        raw = yaml.safe_load(path.read_text()) or {}
        return cls(
            defaults=raw.get("defaults", {}),
            domains=raw.get("domains", {}) or {},
            image_only=set(raw.get("image_only_panels", []) or []),
        )

    def for_url(self, url: str) -> DomainPolicy:
        domain = registrable_domain(url)
        cfg = {**self._defaults, **(self._domains.get(domain) or {})}
        geo_cfg = cfg.get("geo")
        geo = (float(geo_cfg["lat"]), float(geo_cfg["lng"])) if geo_cfg else None
        rank = cfg.get("rank")
        return DomainPolicy(
            domain=domain,
            rate_per_min=float(cfg.get("rate_per_min", 6)),
            needs_browser=bool(cfg.get("needs_browser", False)),
            needs_residential=bool(cfg.get("needs_residential", False)),
            cache_ttl_s=int(cfg.get("cache_ttl_s", 86400)),
            respect_robots=bool(cfg.get("respect_robots", True)),
            geo=geo,
            image_only_panel=domain in self._image_only,
            rank=int(rank) if rank is not None else None,
            trust=str(cfg.get("trust", "unknown")),
        )


@lru_cache
def get_policy_book(path: str) -> PolicyBook:
    return PolicyBook.load(Path(path))
