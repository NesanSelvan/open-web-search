"""Per-domain token bucket.

Rate discipline is per *target*, and it is what keeps us welcome. A domain that
allows 20/min gets 20/min; Google gets 2/min and is additionally throttled by the
identity cooldown, which is the stricter of the two.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    capacity: float
    refill_per_s: float
    tokens: float
    updated: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DomainGovernor:
    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep):
        self._buckets: dict[str, _Bucket] = {}
        self._clock = clock
        self._sleep = sleep
        self._guard = asyncio.Lock()

    async def _bucket(self, domain: str, rate_per_min: float) -> _Bucket:
        async with self._guard:
            bucket = self._buckets.get(domain)
            if bucket is None:
                # Burst of one: we never front-load a domain with saved-up credit.
                bucket = _Bucket(
                    capacity=max(1.0, rate_per_min / 4),
                    refill_per_s=rate_per_min / 60.0,
                    tokens=1.0,
                    updated=self._clock(),
                )
                self._buckets[domain] = bucket
            return bucket

    async def acquire(self, domain: str, rate_per_min: float) -> None:
        """Block until this domain may be hit again."""
        bucket = await self._bucket(domain, rate_per_min)
        async with bucket.lock:
            while True:
                now = self._clock()
                elapsed = now - bucket.updated
                bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_s)
                bucket.updated = now
                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return
                await self._sleep((1.0 - bucket.tokens) / bucket.refill_per_s)
