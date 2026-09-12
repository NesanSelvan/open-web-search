"""Identity pool — the reason homemade SERP scraping works at all.

An *identity* is an inseparable triple: a persistent Chrome profile directory, one
residential exit, and a fixed fingerprint. The binding is permanent for the life of
the identity: a profile that hops between IPs is itself a detection signal.

State machine
-------------
    warm --acquire()--> leased --release(OK)------> cooling --(jitter)--> warm
                           |
                           +---release(BLOCKED)---> quarantined --(backoff)--> warm

Two rules that matter more than pool size:
  * per-exit rate discipline — a thousand exits fired flat out get banned exactly
    like one does;
  * jittered cooldown — a perfectly even interval is itself a scored anomaly, so we
    never sleep a constant.
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from app.settings import Settings

# Fingerprints are pinned per identity and never rotated. A profile whose UA or
# viewport changes between sessions looks like a fresh install every time.
_FINGERPRINTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36", 1920, 1080),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36", 1536, 864),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36", 1728, 1117),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36", 1920, 1080),
]


class State(str, enum.Enum):
    WARM = "warm"
    LEASED = "leased"
    COOLING = "cooling"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class Outcome(str, enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"       # CAPTCHA / sorry page / empty results
    ERROR = "error"           # navigation or network failure, not a block


@dataclass(slots=True)
class ProxyConfig:
    server: str
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, url: str) -> "ProxyConfig | None":
        """`http://user:pass@host:port` -> ProxyConfig. Empty string -> None."""
        url = (url or "").strip()
        if not url:
            return None
        scheme, _, rest = url.partition("://")
        if not rest:
            scheme, rest = "http", url
        creds, sep, hostport = rest.rpartition("@")
        if sep:
            username, _, password = creds.partition(":")
        else:
            username = password = None
        return cls(server=f"{scheme}://{hostport}", username=username or None, password=password or None)


@dataclass
class Identity:
    id: str
    profile_dir: Path
    proxy: ProxyConfig | None
    country: str = "US"
    user_agent: str = _FINGERPRINTS[0][0]
    viewport: tuple[int, int] = (1920, 1080)
    locale: str = "en-US"
    timezone: str = "UTC"

    state: State = State.WARM
    ready_at: float = 0.0
    quarantine_level: int = 0
    # Set when Google caught this identity. The browser manager re-runs the
    # profile warm-up before the next search instead of walking a just-scored
    # profile straight back onto the results page.
    needs_warmup: bool = False
    outcomes: deque[Outcome] = field(default_factory=lambda: deque(maxlen=64))

    @property
    def block_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        blocked = sum(1 for o in self.outcomes if o is Outcome.BLOCKED)
        return blocked / len(self.outcomes)


class PoolExhausted(RuntimeError):
    """No identity became available inside the timeout."""


class IdentityPool:
    def __init__(self, identities: list[Identity], settings: Settings, clock=time.monotonic):
        if not identities:
            raise ValueError("identity pool needs at least one identity")
        self._identities = {i.id: i for i in identities}
        self._s = settings
        self._clock = clock
        self._cv = asyncio.Condition()

    # ---------------------------------------------------------------- factory
    @classmethod
    def from_file(cls, settings: Settings, clock=time.monotonic) -> "IdentityPool":
        """Read `id|proxy_url|country` lines. Blank lines and `#` comments ignored."""
        path = settings.identities_file
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Copy config/identities.example.txt and fill in "
                "one line per (profile, residential exit) pair."
            )
        identities: list[Identity] = []
        for n, raw in enumerate(path.read_text().splitlines()):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            ident_id = parts[0]
            proxy = ProxyConfig.parse(parts[1] if len(parts) > 1 else "")
            country = parts[2] if len(parts) > 2 else settings.default_country
            ua, vw, vh = _FINGERPRINTS[n % len(_FINGERPRINTS)]
            profile_dir = settings.profile_root / ident_id
            profile_dir.mkdir(parents=True, exist_ok=True)
            identities.append(
                Identity(
                    id=ident_id,
                    profile_dir=profile_dir,
                    proxy=proxy,
                    country=country,
                    user_agent=ua,
                    viewport=(vw, vh),
                    locale=settings.locale,
                    timezone=settings.timezone,
                )
            )
        return cls(identities, settings, clock)

    # ---------------------------------------------------------------- leasing
    def _promote_ready(self) -> None:
        """Cooling/quarantined identities whose timer expired become warm again."""
        now = self._clock()
        for ident in self._identities.values():
            if ident.state in (State.COOLING, State.QUARANTINED) and now >= ident.ready_at:
                ident.state = State.WARM

    def _pick_warm(self) -> Identity | None:
        self._promote_ready()
        warm = [i for i in self._identities.values() if i.state is State.WARM]
        if not warm:
            return None
        # Least-recently-ready first, so load spreads instead of hammering one exit.
        warm.sort(key=lambda i: i.ready_at)
        return warm[0]

    async def acquire(self, timeout: float | None = None) -> Identity:
        """Wait for a warm identity.

        Two different clocks on purpose. Identity readiness (cooldown, quarantine)
        runs on the injected clock so it can be driven deterministically in tests.
        The caller's *timeout* is wall time — "wait up to 90 seconds" means real
        seconds, and measuring it on the injected clock would spin forever whenever
        that clock is not advancing on its own.
        """
        timeout = self._s.acquire_timeout_s if timeout is None else timeout
        deadline = time.monotonic() + timeout
        async with self._cv:
            while True:
                ident = self._pick_warm()
                if ident is not None:
                    ident.state = State.LEASED
                    return ident
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolExhausted(
                        f"no warm identity within {timeout:.0f}s ({self.health()})"
                    )
                # Wake on the sooner of: someone releases, or the next timer expires.
                try:
                    await asyncio.wait_for(self._cv.wait(), timeout=min(remaining, self._next_timer()))
                except (asyncio.TimeoutError, TimeoutError):
                    pass

    def _next_timer(self) -> float:
        now = self._clock()
        waits = [
            max(0.05, i.ready_at - now)
            for i in self._identities.values()
            if i.state in (State.COOLING, State.QUARANTINED)
        ]
        return min(waits) if waits else 1.0

    async def release(self, ident: Identity, outcome: Outcome) -> None:
        async with self._cv:
            ident.outcomes.append(outcome)
            now = self._clock()

            if outcome is Outcome.BLOCKED:
                ident.needs_warmup = True
                steps = self._s.quarantine_steps
                level = min(ident.quarantine_level, len(steps) - 1)
                ident.state = State.QUARANTINED
                ident.ready_at = now + steps[level]
                ident.quarantine_level = min(ident.quarantine_level + 1, len(steps) - 1)
            else:
                if outcome is Outcome.OK:
                    ident.quarantine_level = 0
                    ident.needs_warmup = False
                ident.state = State.COOLING
                ident.ready_at = now + random.uniform(
                    self._s.cooldown_min_s, self._s.cooldown_max_s
                )

            # An identity that keeps getting caught is burnt. Recycling it just
            # feeds Google more signal — retire it and say so.
            window = self._s.retire_window
            if len(ident.outcomes) >= window:
                recent = list(ident.outcomes)[-window:]
                rate = sum(1 for o in recent if o is Outcome.BLOCKED) / window
                if rate >= self._s.retire_block_rate:
                    ident.state = State.RETIRED

            self._cv.notify_all()

    def pick_shared(self) -> Identity:
        """An identity to SHARE, without leasing it exclusively.

        Search leases: one query per identity, then a cooldown, because the search
        engine scores per-identity request rate. Page fetching has no such rule —
        it is paced per target domain by the governor — so serialising fetches
        behind an exclusive lease throttled work that needed no throttling and
        wasted the tab concurrency a shared browser already gives us.

        Returns the least-recently-used live identity so load still spreads.
        """
        live = [i for i in self._identities.values() if i.state is not State.RETIRED]
        if not live:
            raise PoolExhausted(f"every identity retired ({self.health()})")
        live.sort(key=lambda i: i.ready_at)
        chosen = live[0]
        # Nudge it so repeated picks rotate rather than always returning the same one.
        chosen.ready_at = self._clock()
        return chosen

    # ---------------------------------------------------------------- health
    _LIVE = (State.WARM, State.LEASED, State.COOLING)

    def _usable(self) -> bool:
        return any(i.state in self._LIVE for i in self._identities.values())

    def _ready_in_s(self) -> float | None:
        """Seconds until an identity can search. 0.0 if one can right now,
        None if every identity is retired and waiting will not help."""
        now = self._clock()
        waits = []
        for ident in self._identities.values():
            if ident.state in (State.WARM, State.LEASED):
                waits.append(0.0)
            elif ident.state in (State.COOLING, State.QUARANTINED):
                waits.append(max(0.0, ident.ready_at - now))
        return round(min(waits), 1) if waits else None

    def health(self) -> dict[str, object]:
        # Sweep first. State only advanced inside acquire(), so on an idle box a
        # pool whose timers had all expired kept reporting itself quarantined —
        # health described the last request, not the present.
        self._promote_ready()
        counts: dict[str, int] = {s.value: 0 for s in State}
        for ident in self._identities.values():
            counts[ident.state.value] += 1
        total_outcomes = sum(len(i.outcomes) for i in self._identities.values())
        blocked = sum(
            sum(1 for o in i.outcomes if o is Outcome.BLOCKED)
            for i in self._identities.values()
        )
        return {
            "total": len(self._identities),
            "states": counts,
            "block_rate": round(blocked / total_outcomes, 3) if total_outcomes else 0.0,
            "retired": [i.id for i in self._identities.values() if i.state is State.RETIRED],
            # Can this pool serve a search at all, and if not, when?
            # COOLING is normal operation (seconds); QUARANTINED is minutes to
            # hours, and RETIRED is forever — a pool holding only those is down.
            "usable": self._usable(),
            "ready_in_s": self._ready_in_s(),
        }
