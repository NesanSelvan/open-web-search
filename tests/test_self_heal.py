"""Self-healing: a burnt profile is swapped for a fresh one. No network, no browser."""

import asyncio
import tempfile
from pathlib import Path

from app.search.identity import (
    Identity,
    IdentityPool,
    Outcome,
    State,
    profile_dir_for,
    read_generation,
)
from app.settings import Settings


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(n=2, **kw):
    root = Path(tempfile.mkdtemp())
    s = Settings(
        profile_root=root, cooldown_min_s=20, cooldown_max_s=45,
        quarantine_steps_s="300,900,3600", retire_window=20,
        rotate_after_blocks=kw.pop("rotate_after_blocks", 2), **kw,
    )
    ids = [Identity(id=f"n{i}", profile_dir=root / f"n{i}", proxy=None) for i in range(n)]
    for i in ids:
        i.profile_dir.mkdir()
    clock = Clock()
    return IdentityPool(ids, s, clock=clock), clock, root


async def block(pool, ident, clock):
    ident.state = State.LEASED
    await pool.release(ident, Outcome.BLOCKED)


class TestRotation:
    async def test_first_block_quarantines_as_before(self):
        pool, clock, root = make()
        ident = pool._identities["n0"]
        await block(pool, ident, clock)
        assert ident.state is State.QUARANTINED
        assert ident.generation == 0

    async def test_second_block_in_a_row_moves_to_a_fresh_profile(self):
        pool, clock, root = make()
        ident = pool._identities["n0"]
        await block(pool, ident, clock)
        await block(pool, ident, clock)
        assert ident.generation == 1
        assert ident.profile_dir == root / "n0.g1" and ident.profile_dir.is_dir()
        # Back in rotation after a normal cooldown, not an hour-long quarantine.
        assert ident.state is State.COOLING
        assert ident.ready_at - clock.now <= 45
        assert ident.quarantine_level == 0 and not ident.needs_warmup
        assert pool.health()["rotated_total"] == 1

    async def test_a_success_resets_the_streak(self):
        pool, clock, root = make()
        ident = pool._identities["n0"]
        await block(pool, ident, clock)
        ident.state = State.LEASED
        await pool.release(ident, Outcome.OK)
        await block(pool, ident, clock)
        assert ident.generation == 0, "blocks with a success between are not a burnt profile"

    async def test_generation_survives_a_restart(self):
        pool, clock, root = make()
        ident = pool._identities["n0"]
        await block(pool, ident, clock)
        await block(pool, ident, clock)
        assert read_generation(root, "n0") == 1
        (root / "ids.txt").write_text("n0||IN\n")
        s = Settings(profile_root=root, identities_file=root / "ids.txt")
        reloaded = IdentityPool.from_file(s)
        assert reloaded._identities["n0"].profile_dir == root / "n0.g1"

    async def test_brake_stops_churn_when_the_exit_itself_is_flagged(self):
        pool, clock, root = make(n=4, rotate_max_per_hour=2)
        for ident in pool._identities.values():
            await block(pool, ident, clock)
            await block(pool, ident, clock)
        gens = sorted(i.generation for i in pool._identities.values())
        assert gens == [0, 0, 1, 1]
        # The un-rotated ones fall back to plain quarantine.
        assert sum(i.state is State.QUARANTINED for i in pool._identities.values()) == 2
        clock.now += 3601
        ident = next(i for i in pool._identities.values() if i.generation == 0)
        await block(pool, ident, clock)
        assert ident.generation == 1, "the brake is per hour, not forever"

    async def test_off_by_default(self):
        pool, clock, root = make(rotate_after_blocks=0)
        ident = pool._identities["n0"]
        for _ in range(3):
            await block(pool, ident, clock)
        assert ident.generation == 0

    async def test_keeps_previous_generation_and_prunes_older(self):
        pool, clock, root = make()
        ident = pool._identities["n0"]
        for _ in range(4):  # two rotations: g1, then g2
            await block(pool, ident, clock)
        await asyncio.sleep(0.2)  # pruning runs off the event loop
        assert ident.generation == 2
        assert not (root / "n0").exists()
        assert (root / "n0.g1").is_dir() and (root / "n0.g2").is_dir()

    def test_dir_naming(self):
        assert profile_dir_for(Path("/p"), "n1", 0) == Path("/p/n1")
        assert profile_dir_for(Path("/p"), "n1", 3) == Path("/p/n1.g3")
