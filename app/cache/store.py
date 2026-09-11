"""Two caches, both ours.

  page_cache        url -> html, with a TTL WE set per domain. No vendor decides
                    our freshness, which is the other half of the Swiggy fix: a
                    stale cached copy of a stripped page is worse than no page.

SQLite because it is one file on the VPS, needs no second service, and handles far
more than our write rate. The schema is deliberately portable to Postgres.
"""

from __future__ import annotations

import json
import time
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


CREATE TABLE IF NOT EXISTS serp_cache (
    key         TEXT PRIMARY KEY,
    query       TEXT NOT NULL,
    results_json TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_serp_expires ON serp_cache(expires_at);

"""



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


    async def stats(self) -> dict:
        out: dict[str, int] = {}
        for table in ("page_cache", "serp_cache"):
            cur = await self.db.execute(f"SELECT COUNT(*) AS n FROM {table}")
            row = await cur.fetchone()
            await cur.close()
            out[table] = row["n"] if row else 0
        return out
