"""Measure a candidate proxy provider's real block rate before committing money.

Proxy quality is provider-specific and the working providers are not published —
people who find a clean one stop naming it, because naming it burns it. One team
measured ~60% blocked on exits that were already tainted before they bought them.
So: buy the smallest packet a provider sells, run this, and only then commit.

    python -m scripts.proxy_trial --identities config/identities.trial.txt --queries 100

Reports per-identity and overall block rate. A pool that cannot hold under ~15%
is not worth buying at any price — the retries cost more than the savings.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import Counter
from pathlib import Path

from app.scrape.policy import registrable_domain
from app.search.google import Blocked, ExtractionFailed, _search_once
from app.search.identity import IdentityPool, Outcome, PoolExhausted
from app.settings import Settings

# Ordinary queries, not "test" — a synthetic pattern is its own detection signal.
# Swap these for queries typical of YOUR traffic before trusting the block rate.
QUERIES = [
    "postgres index bloat",
    "rust async runtime comparison",
    "kubernetes pod eviction reasons",
    "webassembly component model",
    "sqlite wal mode concurrency",
    "tls session resumption explained",
    "python asyncio task cancellation",
    "nginx reverse proxy timeouts",
    "docker layer caching tips",
    "grafana alerting best practices",
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identities", default="config/identities.txt")
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--wait", type=float, default=420.0,
                    help="seconds to wait for a warm identity before stopping early")
    args = ap.parse_args()

    settings = Settings(identities_file=Path(args.identities))
    pool = IdentityPool.from_file(settings)

    per_identity: dict[str, Counter] = {}
    steady: Counter = Counter()          # everything except each profile's first query
    first_use_blocked = 0
    started = time.perf_counter()

    for n in range(args.queries):
        query = random.choice(QUERIES)
        try:
            # A quarantined identity is out for 5 minutes on a first block, so a
            # small trial pool must be willing to wait it out rather than die.
            ident = await pool.acquire(timeout=args.wait)
        except PoolExhausted as exc:
            print(f"\nstopping early — {exc}", flush=True)
            break

        counter = per_identity.setdefault(ident.id, Counter())
        is_first_use = sum(counter.values()) == 0
        try:
            results = await _search_once(ident, query, args.limit, settings, referer=None)
            outcome = Outcome.OK if results else Outcome.BLOCKED
            counter["ok" if results else "blocked"] += 1
            hosts = ", ".join(sorted({registrable_domain(r.url) for r in results})[:4])
            status = f"ok  {len(results):2d} results  [{hosts}]"
        except ExtractionFailed as exc:
            # Our parser, not their block — must not count against the provider.
            outcome = Outcome.ERROR
            counter["parse_error"] += 1
            status = f"PARSE-ERROR  {exc}"
        except Blocked as exc:
            outcome = Outcome.BLOCKED
            counter["blocked"] += 1
            status = f"BLOCKED  {exc}"
        except Exception as exc:
            outcome = Outcome.ERROR
            counter["error"] += 1
            status = f"error  {type(exc).__name__}: {exc}"

        await pool.release(ident, outcome)
        key = "blocked" if outcome is Outcome.BLOCKED else "ok"
        if is_first_use:
            if outcome is Outcome.BLOCKED:
                first_use_blocked += 1
        else:
            steady[key] += 1
        marker = " (first use)" if is_first_use else ""
        print(f"[{n + 1:3d}/{args.queries}] {ident.id:10} {status}{marker}", flush=True)

    elapsed = time.perf_counter() - started
    total = Counter()
    for counts in per_identity.values():
        total.update(counts)

    attempts = sum(total.values())
    # Parse errors are OUR bug and must not be charged to the provider.
    scored = attempts - total["parse_error"]
    block_rate = total["blocked"] / scored if scored else 0.0

    steady_attempts = sum(v for k, v in steady.items())
    steady_blocked = steady.get("blocked", 0)
    steady_rate = steady_blocked / steady_attempts if steady_attempts else 0.0

    print("\n--- per identity ---")
    for ident_id, counts in sorted(per_identity.items()):
        n = sum(counts.values())
        rate = counts["blocked"] / n if n else 0.0
        print(
            f"{ident_id:10} n={n:3d}  blocked={rate:6.1%}  ok={counts['ok']:3d}  "
            f"err={counts['error']:3d}  parse_err={counts['parse_error']:3d}"
        )

    print("\n--- overall ---")
    print(f"first-use    {first_use_blocked} of {len(per_identity)} profiles blocked on their FIRST query")
    print(f"attempts    {attempts}  (scored {scored}, parse errors {total['parse_error']} excluded)")
    print(f"ok          {total['ok']}")
    print(f"block rate  {block_rate:.1%}  (all queries)")
    print(f"steady rate {steady_rate:.1%}  (excluding each profile's first query)")
    print(f"elapsed     {elapsed / 60:.1f} min  ({attempts / max(elapsed / 3600, 1e-9):.0f}/hr sustained)")
    print(f"pool        {pool.health()}")

    # Judge the EXIT on steady-state behaviour. A block on a profile's first query
    # is a cold-profile symptom, not a dirty IP — measured on a fresh box, every
    # block landed on a first query while one warmed profile went 9-for-9.
    if steady_rate <= 0.15:
        if first_use_blocked:
            print(
                f"\nVERDICT: exit is usable ({steady_rate:.1%} steady). "
                f"{first_use_blocked} cold-profile block(s) — profiles are warmed on "
                "first use, so this cost is paid once per profile, not per query."
            )
        else:
            print("\nVERDICT: usable. Buy a larger packet and add identities.")
        return 0
    print(
        f"\nVERDICT: too dirty — {steady_rate:.1%} blocked even after warm-up. "
        "Retries will cost more than the savings; try another provider."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
