"""Two caches, both ours.

  page_cache        url -> html, with a TTL WE set per domain. No vendor decides
                    our freshness, which is the other half of the Swiggy fix: a
                    stale cached copy of a stripped page is worse than no page.
  resolution_cache  normalised food name -> panel. This is what turns a repeat
                    lookup from ~7 seconds into ~15 milliseconds.

SQLite because it is one file on the VPS, needs no second service, and handles far
more than our write rate. The schema is deliberately portable to Postgres.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS page_cache (
    url         TEXT PRIMARY KEY,
    final_url   TEXT,
    html        TEXT NOT NULL,
    status      INTEGER NOT NULL,
    track       TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_expires ON page_cache(expires_at);

CREATE TABLE IF NOT EXISTS resolution_cache (
    key         TEXT PRIMARY KEY,
    food_name   TEXT NOT NULL,
    panel_json  TEXT NOT NULL,
    resolved_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resolution_expires ON resolution_cache(expires_at);

CREATE TABLE IF NOT EXISTS serp_cache (
    key         TEXT PRIMARY KEY,
    query       TEXT NOT NULL,
    results_json TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_serp_expires ON serp_cache(expires_at);

CREATE TABLE IF NOT EXISTS resolution_misses (
    key         TEXT PRIMARY KEY,
    food_name   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    seen_at     REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 1
);
"""

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def normalise_name(name: str, brand: str | None = None) -> str:
    """Cache key. Matches the food-db normaliser's shape so keys agree with corpus keys."""
    raw = f"{brand or ''} {name}".strip()
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    return _PUNCT_RE.sub(" ", folded.lower()).strip()


class CacheStore:
    def __init__(self, path: Path):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        # WAL lets the reader threads run while a scrape worker writes.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("CacheStore.open() was never awaited")
        return self._db

    # ------------------------------------------------------------ page cache
    async def get_page(self, url: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM page_cache WHERE url = ? AND expires_at > ?", (url, time.time())
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def put_page(
        self, url: str, html: str, status: int, track: str, ttl_s: int, final_url: str | None
    ) -> None:
        if ttl_s <= 0:
            return          # ttl 0 means "never cache this domain" (e.g. google.com)
        now = time.time()
        await self.db.execute(
            "INSERT INTO page_cache (url, final_url, html, status, track, fetched_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
            "final_url=excluded.final_url, html=excluded.html, status=excluded.status, "
            "track=excluded.track, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
            (url, final_url, html, status, track, now, now + ttl_s),
        )
        await self.db.commit()

    # ------------------------------------------------------ resolution cache
    async def get_resolution(self, key: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM resolution_cache WHERE key = ? AND expires_at > ?", (key, time.time())
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        return json.loads(row["panel_json"])

    async def put_resolution(self, key: str, food_name: str, panel: dict, ttl_s: int) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO resolution_cache (key, food_name, panel_json, resolved_at, expires_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "panel_json=excluded.panel_json, resolved_at=excluded.resolved_at, "
            "expires_at=excluded.expires_at",
            (key, food_name, json.dumps(panel), now, now + ttl_s),
        )
        await self.db.commit()

    # -------------------------------------------------------------- serp cache
    async def get_serp(self, key: str) -> list[dict] | None:
        cur = await self.db.execute(
            "SELECT results_json FROM serp_cache WHERE key = ? AND expires_at > ?",
            (key, time.time()),
        )
        row = await cur.fetchone()
        await cur.close()
        return json.loads(row["results_json"]) if row else None

    async def put_serp(self, key: str, query: str, results: list[dict], ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        now = time.time()
        await self.db.execute(
            "INSERT INTO serp_cache (key, query, results_json, fetched_at, expires_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "results_json=excluded.results_json, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (key, query, json.dumps(results), now, now + ttl_s),
        )
        await self.db.commit()

    async def record_miss(self, key: str, food_name: str, reason: str) -> None:
        """Misses are the backlog worth working: they name the gaps in the corpus."""
        await self.db.execute(
            "INSERT INTO resolution_misses (key, food_name, reason, seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET attempts = attempts + 1, seen_at = excluded.seen_at, "
            "reason = excluded.reason",
            (key, food_name, reason, time.time()),
        )
        await self.db.commit()

    async def stats(self) -> dict:
        out: dict[str, int] = {}
        for table in ("page_cache", "serp_cache", "resolution_cache", "resolution_misses"):
            cur = await self.db.execute(f"SELECT COUNT(*) AS n FROM {table}")
            row = await cur.fetchone()
            await cur.close()
            out[table] = row["n"] if row else 0
        return out
