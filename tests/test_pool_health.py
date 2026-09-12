"""What /health is allowed to claim about the pool.

The production outage this covers: both identities were quarantined, every
/search 503'd, and /health still answered `ok: true` — so nothing alerted.
"""

import pytest

from app.search.identity import Identity, IdentityPool, Outcome, State
from app.settings import Settings


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pool(n: int, clock: FakeClock, **kw) -> IdentityPool:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    settings = Settings(
        cooldown_min_s=20.0,
        cooldown_max_s=45.0,
        quarantine_steps_s="300,1800,14400",
        retire_block_rate=0.4,
        retire_window=5,
        acquire_timeout_s=0.2,
        **kw,
    )
    idents = [Identity(id=f"id{i}", profile_dir=root / f"id{i}", proxy=None) for i in range(n)]
    return IdentityPool(idents, settings, clock=clock)


class TestHealthTellsTheTruth:
    async def test_expired_quarantine_is_not_reported_as_quarantined(self):
        """health() must sweep. Otherwise an idle box shows a pool that is dead
        while every timer has already expired — only acquire() promoted."""
        clock = FakeClock()
        pool = make_pool(1, clock)
        ident = await pool.acquire()
        await pool.release(ident, Outcome.BLOCKED)
        assert pool.health()["states"]["quarantined"] == 1

        clock.advance(301)
        h = pool.health()
        assert h["states"]["quarantined"] == 0
        assert h["states"]["warm"] == 1

    async def test_ready_in_s_is_zero_while_an_identity_is_warm(self):
        pool = make_pool(2, FakeClock())
        assert pool.health()["ready_in_s"] == 0.0

    async def test_ready_in_s_counts_down_to_the_soonest_identity(self):
        clock = FakeClock()
        pool = make_pool(2, clock)

        first = await pool.acquire()
        await pool.release(first, Outcome.BLOCKED)      # +300
        second = await pool.acquire()
        clock.advance(100)
        await pool.release(second, Outcome.BLOCKED)     # +300 from now

        h = pool.health()
        assert h["states"]["quarantined"] == 2
        assert h["ready_in_s"] == pytest.approx(200.0)  # the first one, 100s in

    async def test_usable_is_false_only_when_every_identity_is_parked(self):
        clock = FakeClock()
        pool = make_pool(2, clock)
        assert pool.health()["usable"] is True

        for _ in range(2):
            ident = await pool.acquire()
            await pool.release(ident, Outcome.BLOCKED)
        assert pool.health()["usable"] is False

    async def test_cooling_still_counts_as_usable(self):
        """Cooldown is seconds and is normal operation — not an outage."""
        clock = FakeClock()
        pool = make_pool(1, clock)
        ident = await pool.acquire()
        await pool.release(ident, Outcome.OK)
        assert ident.state is State.COOLING
        assert pool.health()["usable"] is True


class TestReWarmAfterBlock:
    async def test_block_flags_the_identity_for_re_warming(self):
        """A blocked profile is a profile Google has just scored. Sending it
        straight back into /search when quarantine lifts repeats the mistake."""
        pool = make_pool(1, FakeClock())
        ident = await pool.acquire()
        await pool.release(ident, Outcome.BLOCKED)
        assert ident.needs_warmup is True

    async def test_clean_search_does_not_flag_warmup(self):
        pool = make_pool(1, FakeClock())
        ident = await pool.acquire()
        await pool.release(ident, Outcome.OK)
        assert ident.needs_warmup is False

    async def test_error_outcome_does_not_flag_warmup(self):
        """A navigation failure is our problem, not a detection event."""
        pool = make_pool(1, FakeClock())
        ident = await pool.acquire()
        await pool.release(ident, Outcome.ERROR)
        assert ident.needs_warmup is False
