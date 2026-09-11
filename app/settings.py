"""Runtime configuration. Everything is env-driven so nothing secret lands in git."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WS_", env_file=".env", extra="ignore")

    # service
    # The one key every caller sends as `X-API-Key`. No default on purpose: the
    # service refuses to start until it is set (see app.main).
    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # identity pool
    identities_file: Path = Path("config/identities.txt")
    profile_root: Path = Path("./profiles")
    cooldown_min_s: float = 20.0
    cooldown_max_s: float = 45.0
    quarantine_steps_s: str = "300,1800,14400"
    retire_block_rate: float = 0.4
    retire_window: int = 20
    acquire_timeout_s: float = 90.0

    # chrome
    # Each live context is a full Chrome (~400-600MB). Keeping one per identity
    # traded latency for memory and OOM-killed the process on a dev laptop; on a
    # 4GB VPS it would take the box. Cap the open set and evict idle ones — a
    # relaunch costs ~1.3s, which is worth paying occasionally rather than always.
    max_open_contexts: int = 2
    # Concurrent tabs per Chrome. Tabs are the cheap axis of concurrency —
    # ~55ms and tens of MB each, against ~1330ms and ~1GB for another browser.
    max_tabs_per_context: int = 6
    context_idle_ttl_s: float = 120.0
    chrome_channel: str = "chrome"
    headless: bool = False

    # --- locale / market -----------------------------------------------------
    # Defaults are deliberately neutral. A deployment targeting one market sets
    # these; the engine has no opinion about which market you are searching.
    locale: str = "en-US"
    timezone: str = "UTC"
    accept_language: str = "en-US,en;q=0.9"
    default_country: str = "US"

    # Appended to every bare query. Empty by default: a search engine searches what
    # you typed. Set it to steer a whole deployment at one corpus (e.g. a site: or
    # filetype: hint, or a recurring qualifier), or send `raw: true` per request to
    # bypass query building entirely.
    query_suffix: str = ""

    # search
    search_max_retries: int = 3
    search_results: int = 10
    # A repeated query must not spend an identity. Results for a given query rarely
    # churn hour to hour, so this can be generous.
    serp_cache_ttl_s: int = 6 * 3600
    # Pause on the results page before reading it. Jitter matters more than length.
    dwell_min_s: float = 0.15
    dwell_max_s: float = 0.45

    # --- concurrency ---------------------------------------------------------
    # How many requests run at once, and how many may wait. Past both, a caller is
    # refused immediately with Retry-After instead of discovering after a long
    # timeout that it was never going to be served.
    max_in_flight: int = 8
    max_queued: int = 32
    overload_retry_after_s: float = 5.0
    # Collapse concurrent identical work. A cache only helps AFTER the first call
    # finishes, so a burst of identical requests is exactly what it cannot catch.
    coalesce_requests: bool = True

    # scrape
    # A separate identity pool, deliberately. Search identities must not be spent
    # on scraping: two Chrome instances cannot share one profile directory, and
    # queueing page fetches behind the search engine's 30s cooldown makes a request
    # take two minutes instead of eight seconds. Content sites need nothing like that
    # pacing — the per-domain governor does that work instead.
    scrape_identities_file: Path = Path("config/identities.scrape.txt")
    scrape_cooldown_min_s: float = 0.5
    scrape_cooldown_max_s: float = 2.0
    scrape_concurrency: int = 5
    default_geo_lat: float = 12.9716
    default_geo_lng: float = 77.5946

    # cache
    db_path: Path = Path("./cache.db")

    # config
    domains_file: Path = Path("config/domains.yaml")

    @property
    def quarantine_steps(self) -> list[float]:
        return [float(x) for x in self.quarantine_steps_s.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
