"""Recovering from a dead Playwright driver.

The driver is one Node process. When it dies (2026-09-18: a zombie leak hit the
container pid cap, Chrome could not spawn a thread, the driver asserted and
exited) every identity fails with the same transport error and nothing
relaunches it. The manager must recognise that error, start a fresh driver once,
and let the next attempt launch Chrome on it. No real browser here.
"""

import tempfile
from pathlib import Path

import pytest

from app.search import browser as browser_mod
from app.search.browser import BrowserManager, driver_dead
from app.search.identity import Identity
from app.settings import Settings


DEAD = RuntimeError(
    "Page.goto: unable to perform operation on <WriteUnixTransport closed=True "
    "reading=False 0x7c5eb5101700>; the handler is closed"
)


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeContext:
    pages: list = []

    async def new_page(self):
        return object()


@pytest.fixture
def ident() -> Identity:
    root = Path(tempfile.mkdtemp())
    (root / "p0").mkdir()
    (root / "p0" / ".warmed").write_text("warmed\n")
    return Identity(id="p0", profile_dir=root / "p0", proxy=None)


@pytest.fixture
def manager(monkeypatch) -> BrowserManager:
    started: list[FakePlaywright] = []

    class Starter:
        async def start(self):
            pw = FakePlaywright()
            started.append(pw)
            return pw

    monkeypatch.setattr(browser_mod, "async_playwright", lambda: Starter())
    m = BrowserManager(Settings(api_key="k", max_open_contexts=4))
    m.started = started                                    # type: ignore[attr-defined]
    return m


class TestDetection:
    def test_transport_and_driver_errors_are_driver_death(self):
        assert driver_dead(DEAD)
        assert driver_dead(RuntimeError("Connection closed while reading from the driver"))

    def test_an_ordinary_navigation_error_is_not(self):
        assert not driver_dead(RuntimeError("Page.goto: Timeout 30000ms exceeded"))
        assert not driver_dead(RuntimeError("Page.goto: Page crashed"))


class TestRestart:
    async def test_restart_stops_the_old_driver_and_starts_a_new_one(self, manager):
        await manager.start()
        old = manager.started[0]
        manager._contexts["p0"] = FakeContext()
        manager._search_tabs["p0"] = object()

        await manager.restart("test")

        assert old.stopped
        assert len(manager.started) == 2
        assert manager.driver_alive()
        assert manager._contexts == {} and manager._search_tabs == {}
        assert manager.generation == 1

    async def test_many_failures_on_one_dead_driver_restart_once(self, manager):
        await manager.start()
        gen = manager.generation
        for _ in range(5):
            assert await manager.note_failure(DEAD, gen)
        assert len(manager.started) == 2                   # one restart, not five

    async def test_unrelated_failure_does_not_restart(self, manager):
        await manager.start()
        assert not await manager.note_failure(RuntimeError("Timeout"), manager.generation)
        assert len(manager.started) == 1

    async def test_context_relaunches_after_a_dead_driver_launch(self, manager, monkeypatch, ident):
        await manager.start()
        calls: list[FakePlaywright] = []

        async def fake_launch(pw, ident, settings):
            calls.append(pw)
            if len(calls) == 1:
                raise DEAD
            return FakeContext()

        monkeypatch.setattr(browser_mod, "_launch", fake_launch)

        ctx = await manager.context(ident)

        assert isinstance(ctx, FakeContext)
        assert len(calls) == 2
        assert calls[0] is not calls[1]                    # second launch on the new driver
        assert manager.started[0].stopped

    async def test_ready_state_is_false_before_start(self, manager):
        assert not manager.driver_alive()
        await manager.start()
        assert manager.driver_alive()
