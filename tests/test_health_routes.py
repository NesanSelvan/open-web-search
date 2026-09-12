"""Liveness vs readiness.

`/health` answers "is the process alive" — the container healthcheck reads it,
so it must not start failing because Google parked an identity for five minutes
(an unhealthy container is one autoheal rule away from a restart loop that
throws away the warm Chrome contexts, which makes the outage worse).

`/ready` answers "can this serve a search right now". That is the one a monitor
should page on, and the one that was missing when both identities were
quarantined and /health still said ok.
"""

import types

import pytest
from fastapi import HTTPException

from app.api.routes import health, ready
from app.search.identity import Identity, IdentityPool, Outcome
from app.settings import Settings


def make_request(pool) -> types.SimpleNamespace:
    settings = Settings(api_key="k", max_tabs_per_context=6, max_open_contexts=2)

    async def stats():
        return {"page_cache": 0, "serp_cache": 0}

    services = types.SimpleNamespace(
        pool=pool,
        settings=settings,
        browsers=types.SimpleNamespace(open_contexts=lambda: 0, open_tabs=lambda: 0),
        admission=types.SimpleNamespace(stats=lambda: {"in_flight": 0}),
        searcher=types.SimpleNamespace(_flight=types.SimpleNamespace(stats=lambda: {})),
        cache=types.SimpleNamespace(stats=stats),
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(services=services)))


def make_pool(n: int = 2) -> IdentityPool:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    settings = Settings(quarantine_steps_s="300,1800", acquire_timeout_s=0.2)
    idents = [Identity(id=f"id{i}", profile_dir=root / f"id{i}", proxy=None) for i in range(n)]
    return IdentityPool(idents, settings)


async def quarantine_all(pool: IdentityPool) -> None:
    while True:
        try:
            ident = await pool.acquire(timeout=0.01)
        except Exception:
            return
        await pool.release(ident, Outcome.BLOCKED)


class TestHealth:
    async def test_healthy_pool_reports_ok(self):
        body = await health(make_request(make_pool()))
        assert body["ok"] is True
        assert body["status"] == "ok"

    async def test_stays_200_but_says_degraded_when_every_identity_is_parked(self):
        pool = make_pool()
        await quarantine_all(pool)
        body = await health(make_request(pool))

        assert body["status"] == "degraded"
        assert body["ok"] is False          # the field that was lying during the outage
        assert body["identity_pool"]["ready_in_s"] > 0


class TestReady:
    async def test_ready_returns_the_wait_when_servable(self):
        body = await ready(make_request(make_pool()))
        assert body["ready"] is True
        assert body["ready_in_s"] == 0.0

    async def test_ready_503s_with_retry_after_when_pool_is_parked(self):
        pool = make_pool()
        await quarantine_all(pool)

        with pytest.raises(HTTPException) as exc:
            await ready(make_request(pool))

        assert exc.value.status_code == 503
        # A caller that is told to come back in 300s stops hammering a pool that
        # cannot answer — which is itself part of why it got blocked.
        assert int(exc.value.headers["Retry-After"]) == 300


class TestSearchUnavailable:
    """The 503 a caller actually meets when the pool is empty."""

    async def test_carries_retry_after_from_the_pool_timer(self):
        from app.api.routes import _unavailable

        pool = make_pool()
        await quarantine_all(pool)
        exc = _unavailable(pool, "no warm identity within 15s")

        assert exc.status_code == 503
        assert int(exc.headers["Retry-After"]) == 300

    async def test_no_header_when_every_identity_is_retired(self):
        """Nothing to wait for — retiring is permanent, so promising a time
        would be a lie. The caller gets the 503 without a Retry-After."""
        from app.api.routes import _unavailable
        from app.search.identity import State

        pool = make_pool(1)
        for ident in pool._identities.values():
            ident.state = State.RETIRED
        exc = _unavailable(pool, "every identity retired")

        assert exc.status_code == 503
        assert not exc.headers
