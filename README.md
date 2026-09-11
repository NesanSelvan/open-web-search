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
     └─ extract  optional. Pluggable readers turn a page into structured data
```

| Endpoint | Does |
|---|---|
| `POST /search` | search; optionally scrape and extract every result in the same call |
| `POST /scrape` | one URL → markdown / links / html |
| `POST /map` | enumerate a domain from its sitemap |
| `POST /resolve` | search + scrape + extract + pick the best answer, in one call |
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

## Extraction is pluggable

The engine returns markdown. Turning a page into *structured* data is a plugin, and
this repo ships nutrition panels as the worked example: readers for several
retailer and composition-table layouts, a generic reader for any page printing a
table, and a schema.org `NutritionInformation` reader.

A reader is a pure function over `(html, markdown, url)` — no network, no clock —
so every one is tested against a saved page in CI. Adding a site is one file, one
fixture, one test. Most sites need no reader at all.

**There is no model in the read path.** The hosted extractor this replaced invented
data: a complete macro panel for a page that printed none, a protein figure lifted
out of a meta description, another lifted out of a product title. A guessed value
is worse than no value, because nothing downstream can tell the difference. Here a
page with no readable table returns `no_panel`, and the miss list names exactly
which reader to write next.

Guards that exist because real pages broke them:

- Absent value is `null`, **never `0`**. A literally printed `0.0` is the one case
  where zero is faithful.
- Basis is read off the page, never assumed. Per-serving is converted only when the
  serving weight is actually printed.
- Units are converted using the page's own unit column — `912.75 mg` of saturated
  fat is 0.9 g, not 912 g.
- A table listing both per-100g and per-serving rows must not let the second
  silently overwrite the first.
- Physically impossible values are dropped and named: no macro above 100 g/100 g,
  no energy above 900 kcal/100 g. One source had duplicated its polyunsaturated-fat
  figure into its cholesterol row.

## Nothing about one market is baked in

Results open in the **search engine's own order** — it already ranked them, and
overriding that with a hand-written preference list is an opinion an engine should
not hold. Everything market-specific is config:

| Concern | Where |
|---|---|
| Rate limits, browser need, geo, cache TTL, robots | per domain in `config/domains.yaml` |
| What a source's data is worth | `trust:` per domain (`lab`/`brand`/`retailer`/`aggregator`) |
| Result ordering | `rank:` per domain — **omit it** to use the engine's order |
| Locale, timezone, Accept-Language, country | `WS_LOCALE`, `WS_TIMEZONE`, `WS_ACCEPT_LANGUAGE`, `WS_DEFAULT_COUNTRY` |
| What a bare query gets expanded to | `WS_QUERY_SUFFIX`, or send `raw: true` |

The shipped `domains.yaml` names exactly one domain, and only because search
engines need slower pacing than content sites.
`config/domains.india.example.yaml` shows what a market overlay looks like.

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
python -m pytest      # 104 tests, no network and no browser
```

Readers run against saved pages, the identity pool runs on a fake clock, and every
bug that once corrupted a value has a regression test.

## Honest limits

- **Scraping search results is against major search engines' terms.** This
  automates a browser; `respect_robots` is per domain so the choice is explicit and
  yours. You are responsible for how you operate it.
- Sustained throughput needs identities, and identities need RAM. One box is not a
  crawl farm.
- ~8% of searches are blocked and retried on another identity. That is the normal
  operating point, not a bug to chase to zero.
- Pages whose data exists only inside an image are refused, not guessed at. Vision
  is not implemented.

MIT licensed. See [LICENSE](LICENSE) for fixture provenance and usage notes.
