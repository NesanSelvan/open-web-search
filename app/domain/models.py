"""Core value objects. No I/O, no framework — safe to import anywhere."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SearchResult:
    """One organic result from the search engine."""

    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0


@dataclass(slots=True)
class Page:
    """A fetched page.

    `track` records HOW it was fetched, and it is the field worth watching: a
    fetch that lands on "static" cost ~300ms, one on "browser" cost seconds. A
    shift from static to browser across the board means something upstream is
    failing silently — which is exactly how a misconfigured HTTP client once hid,
    escalating every page to a browser while looking merely slow.
    """

    url: str
    html: str
    status: int = 200
    fetched_at: float = 0.0
    track: str = "static"          # static | browser | cache
    final_url: str | None = None
