"""Identity pool state machine, on a fake clock. No browser, no network."""

import pytest

from app.search.identity import (
    Identity,
    IdentityPool,
    Outcome,
    PoolExhausted,
    ProxyConfig,
    State,
)
from app.settings import Settings


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_settings(**kw) -> Settings:
    defaults = dict(
        cooldown_min_s=20.0,
        cooldown_max_s=45.0,
        quarantine_steps_s="300,1800,14400",
        retire_block_rate=0.4,
        retire_window=5,
        acquire_timeout_s=0.2,
    )
    return Settings(**{**defaults, **kw})


def make_pool(n: int, clock: FakeClock, **kw) -> IdentityPool:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    identities = [
        Identity(id=f"id{i}", profile_dir=root / f"id{i}", proxy=None) for i in range(n)
    ]
    return IdentityPool(identities, make_settings(**kw), clock=clock)


class TestProxyParsing:
    def test_parses_authenticated_url(self):
        cfg = ProxyConfig.parse("http://user:pass@gate.example.net:7777")
        assert cfg.server == "http://gate.example.net:7777"
        assert cfg.username == "user"
        assert cfg.password == "pass"

    def test_parses_unauthenticated_url(self):
        cfg = ProxyConfig.parse("http://gate.example.net:7777")
        assert cfg.server == "http://gate.example.net:7777"
        assert cfg.username is None

    def test_empty_means_host_ip(self):
        assert ProxyConfig.parse("") is None


class TestLeasing:
    async def test_acquire_marks_leased(self):
        pool = make_pool(1, FakeClock())
        ident = await pool.acquire()
        assert ident.state is State.LEASED

    async def test_ok_release_enters_cooldown_not_immediately_reusable(self):
        clock = FakeClock()
        pool = make_pool(1, clock)
        ident = await pool.acquire()
        await pool.release(ident, Outcome.OK)

        assert ident.state is State.COOLING
        with pytest.raises(PoolExhausted):
            await pool.acquire(timeout=0.05)

    async def test_cooldown_is_jittered_within_bounds(self):
        clock = FakeClock()
        pool = make_pool(1, clock)
        waits = []
        for _ in range(20):
            ident = await pool.acquire()
            await pool.release(ident, Outcome.OK)
            waits.append(ident.ready_at - clock.now)
            clock.advance(60)
        # A constant interval is itself a scored anomaly, so this must vary.
        assert min(waits) >= 20.0 and max(waits) <= 45.0
        assert len(set(round(w, 3) for w in waits)) > 1

    async def test_identity_returns_after_cooldown(self):
        clock = FakeClock()
        pool = make_pool(1, clock)
        ident = await pool.acquire()
        await pool.release(ident, Outcome.OK)
        clock.advance(46)
        again = await pool.acquire(timeout=0.1)
        assert again.id == ident.id


class TestQuarantine:
    async def test_block_quarantines_for_first_step(self):
        clock = FakeClock()
        pool = make_pool(1, clock)
        ident = await pool.acquire()
        await pool.release(ident, Outcome.BLOCKED)

        assert ident.state is State.QUARANTINED
        assert ident.ready_at == clock.now + 300

    async def test_backoff_escalates_per_identity(self):
        clock = FakeClock()
        pool = make_pool(1, clock)
        expected = [300, 1800, 14400, 14400]
        for step in expected:
            ident = await pool.acquire(timeout=0.1)
            await pool.release(ident, Outcome.BLOCKED)
            assert ident.ready_at == clock.now + step
            clock.advance(step + 1)

    async def test_success_resets_backoff(self):
        clock = FakeClock()
        pool = make_pool(1, clock)

        ident = await pool.acquire()
        await pool.release(ident, Outcome.BLOCKED)
        clock.advance(301)

        ident = await pool.acquire(timeout=0.1)
        await pool.release(ident, Outcome.OK)
        clock.advance(46)

        ident = await pool.acquire(timeout=0.1)
        await pool.release(ident, Outcome.BLOCKED)
        assert ident.ready_at == clock.now + 300     # back to step one


class TestRetirement:
    async def test_persistently_blocked_identity_is_retired(self):
        # Recycling a burnt exit just feeds the detector more signal.
        # Retirement is judged per identity over its OWN last N outcomes, so this
        # uses a single-identity pool — with two, the leases split and neither
        # reaches the window.
        clock = FakeClock()
        pool = make_pool(1, clock, retire_window=3, retire_block_rate=0.4)

        target = None
        for _ in range(3):
            ident = await pool.acquire(timeout=0.1)
            target = target or ident
            await pool.release(ident, Outcome.BLOCKED)
            clock.advance(20000)

        assert target.state is State.RETIRED
        assert target.id in pool.health()["retired"]

    async def test_occasional_blocks_do_not_retire(self):
        # 1 block in 5 is 20%, under the 40% threshold — a healthy identity must
        # survive the ordinary failure rate.
        clock = FakeClock()
        pool = make_pool(1, clock, retire_window=5, retire_block_rate=0.4)

        outcomes = [Outcome.OK, Outcome.OK, Outcome.BLOCKED, Outcome.OK, Outcome.OK]
        target = None
        for outcome in outcomes:
            ident = await pool.acquire(timeout=0.1)
            target = target or ident
            await pool.release(ident, outcome)
            clock.advance(20000)

        assert target.state is not State.RETIRED
        assert pool.health()["retired"] == []

    async def test_retired_identity_is_never_leased_again(self):
        clock = FakeClock()
        pool = make_pool(1, clock, retire_window=2, retire_block_rate=0.4)
        for _ in range(2):
            ident = await pool.acquire(timeout=0.1)
            await pool.release(ident, Outcome.BLOCKED)
            clock.advance(20000)

        with pytest.raises(PoolExhausted):
            await pool.acquire(timeout=0.05)


class TestHealth:
    async def test_reports_counts_and_block_rate(self):
        clock = FakeClock()
        pool = make_pool(4, clock)

        a = await pool.acquire()
        await pool.release(a, Outcome.OK)
        b = await pool.acquire()
        await pool.release(b, Outcome.BLOCKED)

        health = pool.health()
        assert health["total"] == 4
        assert health["states"]["warm"] == 2
        assert health["block_rate"] == 0.5


class TestBrowserManagerBounds:
    """A live Chrome context is ~400-600MB. Keeping one per identity forever is a
    memory leak with good latency — it OOM-killed the service in testing."""

    def _manager(self, **kw):
        from app.search.browser import BrowserManager
        return BrowserManager(make_settings(**kw))

    async def test_evicts_least_recently_used_past_the_cap(self):
        import time as _time
        mgr = self._manager(max_open_contexts=2)

        closed: list[str] = []

        class FakeCtx:
            pages = []
            async def close(self): ...

        # Seed three "open" contexts with distinct last-used times.
        mgr._contexts = {"a": FakeCtx(), "b": FakeCtx()}
        now = _time.monotonic()
        mgr._last_used = {"a": now - 100, "b": now - 1}

        async def track(ident_id, reason):
            closed.append(ident_id)
            mgr._contexts.pop(ident_id, None)
            mgr._last_used.pop(ident_id, None)

        mgr._close_one = track
        await mgr._evict_if_needed(keep="c")

        assert closed == ["a"], "must evict the least recently used, not the newest"

    async def test_never_evicts_the_context_being_requested(self):
        mgr = self._manager(max_open_contexts=1)

        class FakeCtx:
            pages = []
            async def close(self): ...

        mgr._contexts = {"a": FakeCtx()}
        mgr._last_used = {"a": 0.0}

        closed: list[str] = []

        async def track(ident_id, reason):
            closed.append(ident_id)
            mgr._contexts.pop(ident_id, None)

        mgr._close_one = track
        await mgr._evict_if_needed(keep="a")

        assert closed == [], "evicting the context we are about to use would loop forever"

    def test_open_contexts_is_reported(self):
        mgr = self._manager()
        assert mgr.open_contexts() == 0
