# Self-hosted web search and scrape

Search the web and read the results, from your own box. Real Google Chrome driving
a real search engine, then fetching and cleaning the pages it found.

```bash
curl -X POST localhost:8080/search \
  -H 'X-Internal-Token: ...' -H 'Content-Type: application/json' \
  -d '{"query":"webassembly component model","limit":5,"scrape":["markdown"]}'
```

Ranked results, each page's content as clean markdown. One VPS, no per-request
billing, no vendor deciding what your traffic looks like.

---

## Why

Hosted search-and-scrape APIs charge per page, browse from *their* location, and
increasingly put a model between you and the page. Self-hosting used to be a losing
fight — a headless browser is blocked by every search engine almost immediately.

That changed. **Headless is a detection class, not a flag**: a screenless browser
reports different screen metrics, different WebGL, and `HeadlessChrome` in its own
user agent. Run *real* Chrome headful against a virtual display and the whole class
of signal disappears.

Measured on a plain datacenter VPS, no proxy:

| | blocked |
|---|---|
| plain `curl` | 7% |
| headless Chrome | **100%** |
| real Chrome, headful under Xvfb, paced | **8.3%** |

The second surprise: on a fresh box, *every* block landed on a profile's **first**
query, while a profile that got through once went 9-for-9. The datacenter IP was
never the problem — a Chrome profile with no cookies, no consent state and no
history was. New profiles are warmed before their first search, and the cold-start
blocks disappear.

## What it does

```
  POST /search
     │
     ├─ SERP cache ───────────────────────────────────── hit ⇒ ~60ms
     │
     ├─ search   real Chrome, headful under Xvfb
     │           one identity per query · jittered cooldown · auto-quarantine
     │
     ├─ scrape   results in parallel, under a hard deadline
     │           plain HTTP first (~300ms) → browser only when needed
     │           per-domain rate limits, geolocation and cache TTL
     │
```

| Endpoint | Does |
|---|---|
| `POST /search` | search; optionally scrape every result in the same call |
| `POST /scrape` | one URL → markdown / links / html |
| `POST /map` | enumerate a domain from its sitemap |
| `GET /health` | identity pool, browsers, cache |

`/search`, `/scrape` and `/map` mirror the request and response shapes of a
well-known hosted API, so existing call sites usually migrate by changing one base
URL.

## Speed

| | |
|---|---|
| Search, new query | **~2.5s** (≈860ms of that is the search engine's own response) |
| Search, repeated | **~60ms** |
| Search + scrape 3 pages | ~3s, bounded by `scrape_deadline_ms` |

Throughput is `identities ÷ cooldown`. Each warm identity holds a live Chrome, so
budget ~1 GB of RAM per identity and set `WS_MAX_OPEN_CONTEXTS` to match — more
identities than the browser cap makes every other request evict and relaunch a
browser, which is exactly the cost the pool exists to avoid.

## Identities

An identity is an inseparable triple: a persistent Chrome profile, a fixed
fingerprint, and optionally one pinned proxy exit. The pairing never changes — a
profile that hops between IPs is itself a signal.

```
warm ──acquire──▶ leased ──ok──▶ cooling ──jitter──▶ warm
                     └──blocked──▶ quarantined ──5m·30m·4h──▶ warm
                                   (>40% blocked ⇒ retired)
```

> **The profile directory is production state.** Profiles accumulate cookies and
> history, and that ordinariness is much of why this works. Back up the state
> volume like a database — losing it resets every identity to cold and suspicious.

**Proxies are optional.** A plain datacenter IP held at ~8% blocked in testing. Add
residential exits when the evidence says to: steady block rate above ~15%, volume
high enough that one IP looks unusual, or a site that serves different content per
country. `scripts/proxy_trial.py` measures a provider before you commit, and
reports cold-profile blocks separately from exit quality — charging a first-use
block to the provider is how you reject a perfectly good exit.


## Nothing about one market is baked in

Results come back in the **search engine's own order** — it already ranked them.
Everything else is per-domain config:

| Concern | Where |
|---|---|
| Rate limits, browser need, geo, cache TTL, robots | per domain in `config/domains.yaml` |
| What a source's data is worth | `trust:` per domain (`lab`/`brand`/`retailer`/`aggregator`) |
| Result ordering | `rank:` per domain — **omit it** to use the engine's order |
| Locale, timezone, Accept-Language, country | `WS_LOCALE`, `WS_TIMEZONE`, `WS_ACCEPT_LANGUAGE`, `WS_DEFAULT_COUNTRY` |
| What a bare query gets expanded to | `WS_QUERY_SUFFIX`, or send `raw: true` |

The shipped `domains.yaml` names exactly one domain, and only because search
engines need slower pacing than content sites.

## Run it

### Docker — installs Chrome for you

```bash
cp .env.example .env                                   # set WS_INTERNAL_TOKEN
cp config/identities.example.txt config/identities.txt
docker compose up -d --build
curl -s localhost:8080/health | jq
```

### Locally — needs real Google Chrome installed

Chromium will not do: `channel="chrome"` is deliberate, because the bundled build
carries its own automation tells. On macOS there is a real display, so Chrome
windows genuinely open — that is the design, not a bug.

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt     # ./venv/bin/pip, not a bare `pip`

cp config/identities.example.txt config/identities.txt
cat > .env <<'EOF'
WS_INTERNAL_TOKEN=local-dev-token
WS_PROFILE_ROOT=./profiles
WS_DB_PATH=./cache.db
WS_COOLDOWN_MIN_S=5
WS_COOLDOWN_MAX_S=12
EOF

./venv/bin/python main.py          # or: python -m app.main
```

`.env.example` points the profile and cache paths at `/var/lib/open-web-search`,
which is right for the container and wrong for a laptop — the block above
overrides both. The shortened cooldown is dev-only: the 20–45s production default
makes back-to-back testing feel like it has hung. Raise it before pointing at real
proxies; a short cooldown with few identities is what burns exits.

Deployment, sizing and the proxy decision: [`deploy/README.md`](deploy/README.md).

```bash
python -m pytest      # no network, no browser
```

The identity pool runs on a fake clock, the rate governor on a fake sleep, and
coalescing and admission control on fake work — so the suite is deterministic and
fast.

## Honest limits

- **Scraping search results is against major search engines' terms.** This
  automates a browser; `respect_robots` is per domain so the choice is explicit and
  yours. You are responsible for how you operate it.
- Sustained throughput needs identities, and identities need RAM. One box is not a
  crawl farm.
- ~8% of searches are blocked and retried on another identity. That is the normal
  operating point, not a bug to chase to zero.
- It returns pages, not answers. Turning a page into structured data is your
  code's job — `/search` with `scrape: ["markdown"]` gives you clean text to work
  from.

MIT licensed. See [LICENSE](LICENSE) for fixture provenance and usage notes.
