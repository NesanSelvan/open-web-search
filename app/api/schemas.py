"""Request/response models.

/search, /scrape and /map deliberately mirror Firecrawl's shapes so existing call
sites migrate by changing a base URL and nothing else.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl

Format = Literal["markdown", "links", "html"]


class SearchRequest(BaseModel):
    # Callers arrive with whatever their existing search endpoint already sends, so
    # `query` / `food_name` / `name` are accepted as aliases rather than making
    # every client rewrite its body. Unknown keys are ignored instead of 422-ing.
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    q: str = Field(
        min_length=1,
        validation_alias=AliasChoices("q", "query", "food_name", "name"),
        description="What to search for. Sent verbatim when raw=true.",
    )
    limit: int = Field(default=10, ge=1, le=20)
    site: str | None = Field(default=None, description="Restrict to one domain, e.g. swiggy.com")
    brand: str | None = None
    raw: bool = Field(default=False, description="Send q to Google verbatim, no query building")
    user_id: str | None = Field(default=None, description="Optional, for request tracing only")

    # Search AND scrape in one call. Empty list = URLs only (fast).
    scrape: list[Format] = Field(
        default_factory=list,
        description="Formats to fetch for each result, e.g. [\"markdown\"]. Empty = no scrape.",
    )
    lat: float | None = None
    lng: float | None = None
    # Hard ceiling on the scrape phase. Pages still in flight when it expires come
    # back as track="timeout" rather than holding the whole response hostage — one
    # slow site should cost that site's result, not the request.
    # Default sized against a measured budget, not a guess: a warm Google search
    # is ~1.5s (859ms of that is Google's own response and cannot be optimised
    # away), so ~1.2s of scrape keeps the whole request under 3s. Raise it when you
    # care more about completeness than latency.
    # Scrape only the first N results; the rest come back as URLs + snippets.
    # Reading a page costs ~0.4s of CPU; a caller that reads two pages should
    # not pay for eight.
    scrape_top: int | None = Field(default=None, ge=1, le=20)
    scrape_deadline_ms: int = Field(default=1200, ge=200, le=60000)


class SearchHit(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0

    # Present only when the request asked to scrape.
    page_title: str | None = None
    status: int | None = None
    track: str | None = None          # static | browser | cache
    final_url: str | None = None
    markdown: str | None = None
    links: list[str] | None = None
    html: str | None = None



class SearchResponse(BaseModel):
    query: str
    results: list[SearchHit]
    # Where this request's time actually went. identity_wait_ms is pool pressure,
    # not search work — if it dominates, add identities rather than tuning code.
    timing_ms: dict | None = None


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    url: HttpUrl
    formats: list[Format] = Field(default_factory=lambda: ["markdown"])
    lat: float | None = None
    lng: float | None = None
    force_fresh: bool = False


class ScrapeResponse(BaseModel):
    url: str
    final_url: str | None = None
    status: int
    track: str
    title: str | None = None
    markdown: str | None = None
    links: list[str] | None = None
    html: str | None = None


class MapRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    domain: str
    search: str | None = None
    limit: int = Field(default=500, ge=1, le=5000)


class MapResponse(BaseModel):
    domain: str
    urls: list[str]



