"""Coalescing and admission control, on fake work — no network, no browser."""

import asyncio

import pytest

from app.concurrency import AdmissionControl, Overloaded, SingleFlight


class TestSingleFlight:
    """Ten concurrent requests for one query used to run ten searches, each
    leasing its own identity. The cache cannot help here: it only fills once the
    first call finishes, which is exactly the window a burst lives in."""

    async def test_concurrent_identical_calls_execute_once(self):
        sf = SingleFlight()
        runs = 0

        async def work():
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)
            return "result"

        results = await asyncio.gather(*(sf.do("same", work) for _ in range(10)))

        assert runs == 1, "the whole point: one execution, not ten"
        assert results == ["result"] * 10
        assert sf.executed == 1
        assert sf.coalesced == 9

    async def test_different_keys_do_not_share(self):
        sf = SingleFlight()
        runs = []

        async def work(tag):
            runs.append(tag)
            await asyncio.sleep(0.01)
            return tag

        out = await asyncio.gather(sf.do("a", lambda: work("a")), sf.do("b", lambda: work("b")))
        assert sorted(out) == ["a", "b"]
        assert sorted(runs) == ["a", "b"]

    async def test_failure_is_shared_not_retried_by_every_waiter(self):
        """If a search was blocked, five waiters immediately retrying would burn
        five more identities on a query already known to be failing."""
        sf = SingleFlight()
        runs = 0

        async def failing():
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.02)
            raise RuntimeError("blocked")

        results = await asyncio.gather(
            *(sf.do("k", failing) for _ in range(5)), return_exceptions=True
        )

        assert runs == 1
        assert all(isinstance(r, RuntimeError) for r in results)

    async def test_key_is_released_so_a_later_call_runs_again(self):
        sf = SingleFlight()
        runs = 0

        async def work():
            nonlocal runs
            runs += 1
            return runs

        assert await sf.do("k", work) == 1
        assert await sf.do("k", work) == 2, "finished work must not be cached forever"

    async def test_a_cancelled_waiter_does_not_kill_the_shared_work(self):
        sf = SingleFlight()
        finished = False

        async def work():
            nonlocal finished
            await asyncio.sleep(0.1)
            finished = True
            return "done"

        leader = asyncio.create_task(sf.do("k", work))
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(sf.do("k", work))
        await asyncio.sleep(0.01)

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert await leader == "done"
        assert finished


class TestAdmissionControl:
    """A burst larger than the pool used to queue on acquire() until a 90s timeout,
    so the client learned nothing for a minute and a half and then got an error."""

    async def test_bounds_concurrent_work(self):
        ac = AdmissionControl(max_in_flight=2, max_queued=10)
        peak = 0
        live = 0

        async def job():
            nonlocal peak, live
            async with ac.slot():
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.03)
                live -= 1

        await asyncio.gather(*(job() for _ in range(8)))
        assert peak == 2
        assert ac.admitted == 8

    async def test_refuses_immediately_once_the_queue_is_full(self):
        ac = AdmissionControl(max_in_flight=1, max_queued=1, retry_after_s=3.0)
        started = asyncio.Event()

        async def hog():
            async with ac.slot():
                started.set()
                await asyncio.sleep(0.2)

        task = asyncio.create_task(hog())
        await started.wait()

        # One waiter is allowed...
        waiter = asyncio.create_task(anext_slot(ac))
        await asyncio.sleep(0.02)

        # ...the next is refused rather than queued.
        with pytest.raises(Overloaded) as exc:
            async with ac.slot():
                pass

        assert exc.value.retry_after_s == 3.0
        assert ac.rejected == 1

        await task
        await waiter

    async def test_rejection_is_fast_not_a_timeout(self):
        ac = AdmissionControl(max_in_flight=1, max_queued=0)
        started = asyncio.Event()

        async def hog():
            async with ac.slot():
                started.set()
                await asyncio.sleep(0.2)

        task = asyncio.create_task(hog())
        await started.wait()

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(Overloaded):
            async with ac.slot():
                pass
        assert loop.time() - t0 < 0.02, "must refuse immediately, not wait it out"
        await task

    async def test_stats_expose_pressure(self):
        ac = AdmissionControl(max_in_flight=2, max_queued=4)
        async with ac.slot():
            stats = ac.stats()
            assert stats["in_flight"] == 1
            assert stats["max_in_flight"] == 2
            assert stats["max_queued"] == 4
        assert ac.stats()["in_flight"] == 0
        assert ac.stats()["peak_in_flight"] == 1


async def anext_slot(ac):
    async with ac.slot():
        await asyncio.sleep(0.01)


class TestSharedBrowserTabs:
    """Tabs are the cheap axis of concurrency: ~55ms and tens of MB each, against
    ~1330ms and ~1GB for another browser. Page fetching is paced per target domain
    by the governor, so it shares a browser rather than leasing one exclusively."""

    def _pool(self, n=2):
        import tempfile
        from pathlib import Path

        from app.search.identity import Identity, IdentityPool
        from app.settings import Settings

        root = Path(tempfile.mkdtemp())
        ids = [Identity(id=f"s{i}", profile_dir=root / f"s{i}", proxy=None) for i in range(n)]
        return IdentityPool(ids, Settings())

    def test_pick_shared_does_not_lease(self):
        from app.search.identity import State

        pool = self._pool()
        a = pool.pick_shared()
        b = pool.pick_shared()
        # Neither call takes the identity out of circulation.
        assert a.state is State.WARM
        assert b.state is State.WARM

    def test_pick_shared_rotates(self):
        pool = self._pool(n=2)
        picks = {pool.pick_shared().id for _ in range(4)}
        assert len(picks) == 2, "load must spread across identities, not pin to one"

    def test_pick_shared_never_returns_a_retired_identity(self):
        import pytest as _pytest

        from app.search.identity import PoolExhausted, State

        pool = self._pool(n=1)
        only = pool.pick_shared()
        only.state = State.RETIRED
        with _pytest.raises(PoolExhausted):
            pool.pick_shared()

    def test_search_still_leases_exclusively(self):
        """Search must NOT share: the engine scores per-identity request rate, and
        the cooldown is the whole reason the pool exists."""
        import asyncio

        from app.search.identity import Outcome, State

        async def check():
            pool = self._pool(n=1)
            ident = await pool.acquire()
            assert ident.state is State.LEASED
            await pool.release(ident, Outcome.OK)
            assert ident.state is State.COOLING

        asyncio.run(check())
