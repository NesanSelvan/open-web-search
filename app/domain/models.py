"""Core value objects. No I/O, no framework — safe to import anywhere."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

# The 31-key per-100g schema, in the exact order food-db already uses.
# Order matters: downstream shard writers rely on it.
PER100_KEYS: tuple[str, ...] = (
    "cal", "prot", "carb", "fat", "sat_fat", "trans_fat", "chol", "fib", "sugar", "na",
    "ca", "fe", "mg", "p", "k", "zn", "cu", "mn", "se", "folate", "omg_3",
    "vit_a", "vit_b1", "vit_b2", "vit_b3", "vit_b6", "vit_b12", "vit_c", "vit_d",
    "vit_e", "vit_k",
)


class Basis(str, enum.Enum):
    PER_100G = "per_100g"
    PER_100ML = "per_100ml"
    PER_SERVING = "per_serving"
    PER_PACK = "per_pack"          # never honest for a single item -> rejected


class RejectReason(str, enum.Enum):
    IMAGE_ONLY = "image_only"       # panel exists but only inside a pack photo
    COMBO_PACK = "combo_pack"       # assorted/variety/A+B -> no single per-100g
    PER_PACK_BASIS = "per_pack_basis"
    NO_PANEL = "no_panel"
    UNPARSEABLE = "unparseable"


class Tier(str, enum.Enum):
    """Source trust. Lower letter wins when two sources disagree."""
    A = "a"   # official lab / composition table
    B = "b"   # printed pack label read as text
    C = "c"   # retailer-transcribed label
    D = "d"   # crowd / API aggregate
    E = "e"   # inferred


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0


@dataclass(slots=True)
class Page:
    url: str
    html: str
    status: int = 200
    fetched_at: float = 0.0
    track: str = "static"          # "static" | "browser" | "cache"
    final_url: str | None = None


@dataclass(slots=True)
class Panel:
    """One product's nutrition, normalised to per-100g.

    An absent nutrient is None — NEVER 0. A literally printed 0.0 is the one case
    where 0 is faithful, and that arrives here as 0.0.
    """
    product: str
    per_100g: dict[str, float | None]
    brand: str | None = None
    serving_g: float | None = None
    per_basis: Basis = Basis.PER_100G
    source_url: str = ""
    source_domain: str = ""
    source_type: str = ""
    tier: Tier = Tier.C
    confidence: float = 0.5
    fetched_at: float = 0.0
    extractor: str = ""            # "adapter:<domain>" | "llm"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "brand": self.brand,
            "per_100g": {k: self.per_100g.get(k) for k in PER100_KEYS},
            "serving_g": self.serving_g,
            "per_basis": self.per_basis.value,
            "source_url": self.source_url,
            "source_domain": self.source_domain,
            "source_type": self.source_type,
            "tier": self.tier.value,
            "confidence": self.confidence,
            "fetched_at": self.fetched_at,
            "extractor": self.extractor,
            "notes": self.notes,
        }


@dataclass(slots=True)
class Rejection:
    reason: RejectReason
    url: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason.value, "url": self.url, "detail": self.detail}
