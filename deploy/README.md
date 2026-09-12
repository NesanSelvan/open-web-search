# Deploying open-web-search

Target: a **dedicated** box. Do not co-locate it with another memory-hungry
service (a search engine, an analytics store): Chrome will fight it for RAM, and
the loser of that fight gets OOM-killed.

## Sizing

Measured, not estimated: one warm Chrome context ≈ **1GB RSS** (2 contexts = 24
processes, ~2.1GB). Everything follows from that:

```
identities  = peak requests/sec × cooldown_seconds
RAM         = identities × ~1GB + ~1.5GB (OS + app + page cache)
```

At a few hundred lookups/day with a 5x burst at peak (~1 per 17s), **2 to 4
identities** is enough. That is 4 to 6GB, so an 8GB box has real headroom.

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

## Shipping an update to a box that is already running

**Do not `scp -r config` onto a live box.** The first-run line above copies
`config/` because the box has none yet; on a running deployment that same line
overwrites `config/identities.txt`, the real identities and their exits, with
whatever the checkout happens to hold (the shipped example is a single
proxy-less `dev1`). The file is bind-mounted read-only into the container, so the
damage shows up as a pool that cannot search.

Ship code only, and leave the box's own config and `.env` alone:

```bash
HOST=root@<HOST>
scp -r app scripts deploy requirements.txt Dockerfile docker-compose.yml $HOST:/opt/websearch/
ssh $HOST 'cd /opt/websearch && docker compose up -d --build'
ssh $HOST 'curl -s -o /dev/null -w "ready:%{http_code}\n" localhost:8080/ready; curl -s localhost:8080/health | jq -c .status,.identity_pool.states'
```

A rebuild restarts Chrome, so every open context is discarded and the in-memory
quarantine timers reset. The profiles themselves live on the `state` volume and
survive, `.warmed` markers included, which is why a restart is not a way
to clear a block: the engine's opinion of the profile is on their side, not ours.

## Configure

**`.env`** — set `WS_API_KEY` to a real secret (`openssl rand -hex 32`). Keep production pacing
(`WS_COOLDOWN_MIN_S=20`, `WS_COOLDOWN_MAX_S=45`); the short dev values exist only so
local testing is not dominated by waiting.

**`config/identities.txt`** — one line per `(profile, exit)` pair.

**Proxies turned out NOT to be required at this volume.** Measured on this box
(a plain datacenter IP, no proxy, 12 real queries across 3 fresh profiles):

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

## Firewall and exposure

`bootstrap.sh` allows only SSH, and the API binds to loopback. Keep both. To reach
it from another machine, put it behind a Cloudflare Tunnel: the connection is
outbound-only, so no inbound port ever opens, and TLS is terminated for you.

```bash
# on the box — https://pkg.cloudflare.com/cloudflared
apt-get install -y cloudflared
cloudflared tunnel login                     # one-time; prints a URL to approve
cloudflared tunnel create web-search
cloudflared tunnel route dns web-search search.example.com
cat > /etc/cloudflared/config.yml <<'EOF'
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: search.example.com
    service: http://localhost:8080
  - service: http_status:404
EOF
cloudflared service install                  # systemd unit, starts on boot
curl -s https://search.example.com/health
```

The API key is then the only lock on the door. Never serve it over plain HTTP, and
treat the key like a database password: long, random, rotated by editing `.env`
and running `docker compose up -d`.

## When the pool goes dark

Every identity quarantined at once, so every `/search` answers

```
503 no warm identity within 15s ({'warm': 0, 'quarantined': 2, ...})
```

while `/health` keeps saying `ok: true`. Nothing alerts, because liveness was
never the thing that broke.

**Run at least 4 identities.** Quarantine is 5 min on the first block, 30 min on
the second, 4 h after that (`WS_QUARANTINE_STEPS_S`). With two identities, one bad
minute from the engine takes the whole service down; with four, the same block
costs 25% of capacity. Keep `WS_MAX_OPEN_CONTEXTS` >= the identity count, and
budget ~1GB RSS per open context.

**Monitor `/ready`, not `/health`.** `/health` is liveness and stays `200` while
degraded on purpose: the container healthcheck reads it, and restarting a
degraded process only discards the warm Chrome contexts. `/ready` returns `503`
with an exact `Retry-After` the moment no identity can search:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/ready    # 200 or 503
curl -s localhost:8080/health | jq '.status, .identity_pool.ready_in_s'
```

There is nothing to do during a quarantine but wait it out. The timer is the
whole point. Restarting the container does **not** clear it faster and loses the
warm contexts. If `retired` is non-empty, that identity is burnt for good: replace
the exit and give it a fresh id in `config/identities.txt`.

## What it returns

Pages, not answers. `/search` gives ranked results and, with
`scrape: ["markdown"]`, each page's content as clean text. Turning that into
structured data is your code's job — the engine deliberately holds no opinion about
what you are extracting.
