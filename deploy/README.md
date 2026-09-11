# Deploying open-web-search

Target: a **dedicated** box. Do not co-locate on the Typesense or PostHog VPS —
Chrome will fight them for RAM, and the Typesense box has already been OOM-killed
once at 23.4GB RSS.

## Sizing

Measured, not estimated: one warm Chrome context ≈ **1GB RSS** (2 contexts = 24
processes, ~2.1GB). Everything follows from that:

```
identities  = peak requests/sec × cooldown_seconds
RAM         = identities × ~1GB + ~1.5GB (OS + app + page cache)
```

At ~667 lookups/day with a 5× meal-time burst (~1 per 17s), **2–4 identities** is
enough. That is 4–6GB, so an 8GB box has real headroom.

`WS_MAX_OPEN_CONTEXTS` **must be ≥ the identity count.** If there are more
identities than the cap, every alternate request evicts a live browser and
relaunches another — ~1.3s, which is exactly the cost the pool exists to avoid.

## Steps

```bash
# 1. key access (run locally, keeps the password out of any transcript)
ssh-copy-id -i ~/.ssh/<your-key>.pub root@<HOST>

# 2. ship the code
ssh root@<HOST> 'mkdir -p /opt/websearch'
scp -r app config scripts deploy requirements.txt Dockerfile docker-compose.yml \
    root@<HOST>:/opt/websearch/

# 3. provision (docker, swap, firewall, state dir)
ssh root@<HOST> 'bash /opt/websearch/deploy/bootstrap.sh'

# 4. configure — see below, then:
ssh root@<HOST> 'cd /opt/websearch && docker compose up -d --build'
ssh root@<HOST> 'curl -s localhost:8080/health'
```

## Configure

**`.env`** — set `WS_INTERNAL_TOKEN` to a real secret. Keep production pacing
(`WS_COOLDOWN_MIN_S=20`, `WS_COOLDOWN_MAX_S=45`); the short dev values exist only so
local testing is not dominated by waiting.

**`config/identities.txt`** — one line per `(profile, exit)` pair.

**Proxies turned out NOT to be required at this volume.** Measured on this box
(a plain Contabo datacenter IP — no proxy — 12 real queries across 3 fresh
profiles):

| | no warm-up | with warm-up |
|---|---|---|
| Profiles blocked on first query | 2 of 3 | **0 of 3** |
| Overall block rate | 16.7% | **8.3%** |
| Sustained | 135/hr | **269/hr** |

Every block in the first run landed on a profile's *first* query; one profile that
got through once then went 9-for-9. The ASN was not the problem — a Chrome profile
with no cookies, no consent state and no history was. `BrowserManager._warm_profile`
now lands each new profile on google.com and settles the consent dialog before it
is allowed to search, and the cold-start blocks disappear.

So start with **no proxies** and watch the block rate on `/health`. Add residential
exits when the evidence says to:

- block rate climbing above ~15% in steady state (the trial reports steady rate
  separately from first-use blocks, precisely because they have different causes);
- daily volume growing enough that one IP's request rate looks unusual;
- quick-commerce domains resolving to the wrong store — those need an *Indian* exit
  for geography, which is a correctness need, not an anti-block one.

If you do buy: quality is not published and varies wildly — one team measured ~60%
blocked on exits already tainted before purchase. Buy the smallest packet and
measure:

  ```bash
  docker compose run --rm web-search python -m scripts.proxy_trial --queries 100
  ```

  Under 15% **steady** blocked: usable. Above it, the retries cost more than the
  savings. Ignore first-use blocks when judging an exit — those are cold profiles.
- Never repoint an existing identity id at a different exit. A profile that hops
  between IPs is itself a detection signal — retire the id and add a new one.

## Back up the profile volume

`/var/lib/open-web-search` holds the Chrome profiles. They accrue cookies and
history, and that accumulated ordinariness is a large part of why the searches
succeed. Losing the volume resets every identity to cold and suspicious, and there
is no quick way to regenerate it. Treat it like a database, not a cache.

## Firewall

`bootstrap.sh` allows only SSH. The API binds to loopback and must stay there —
reach it from your backend over a private network or an SSH tunnel, never by
opening 8080 to the internet. Every route requires `X-Internal-Token`, but that is
a second lock, not the first one.

## What it returns

Pages, not answers. `/search` gives ranked results and, with
`scrape: ["markdown"]`, each page's content as clean text. Turning that into
structured data is your code's job — the engine deliberately holds no opinion about
what you are extracting.
