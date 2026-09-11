#!/usr/bin/env bash
# Provision a fresh Contabo/Debian box for open-web-search.
#
# Idempotent: safe to re-run. Does NOT touch any existing service — this box is
# meant to be dedicated, because Chrome will fight a database for RAM (the
# Typesense box has already been OOM-killed once at 23.4GB).
#
#   scp -r deploy app config requirements.txt Dockerfile docker-compose.yml root@HOST:/opt/websearch/
#   ssh root@HOST 'bash /opt/websearch/deploy/bootstrap.sh'

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/websearch}
STATE_DIR=${STATE_DIR:-/var/lib/open-web-search}

echo "==> sanity: what are we running on?"
echo "    kernel : $(uname -r)  $(uname -m)"
echo "    cpu    : $(nproc) cores"
free -h | awk '/Mem:/ {print "    mem    : " $2 " total, " $7 " available"}'
df -h / | awk 'NR==2 {print "    disk   : " $2 " total, " $4 " free"}'

TOTAL_MB=$(free -m | awk '/Mem:/ {print $2}')
if [ "$TOTAL_MB" -lt 4000 ]; then
  echo "!!  ${TOTAL_MB}MB RAM. Each warm Chrome context measured ~1GB — this box will"
  echo "!!  hold roughly $((TOTAL_MB / 1000 - 1)) identities. Set WS_MAX_OPEN_CONTEXTS accordingly."
fi

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg ufw >/dev/null

echo "==> docker"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  # Docker publishes separate repos per distro; the box may be Ubuntu or Debian.
  DISTRO_ID=$(. /etc/os-release && echo "$ID")
  CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-$UBUNTU_CODENAME}")
  curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${DISTRO_ID} ${CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
docker --version

echo "==> swap (Chrome spikes; a small swap turns an OOM kill into a slow request)"
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap -q /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

echo "==> state dir (Chrome profiles live here — BACK THIS UP)"
mkdir -p "$STATE_DIR/profiles"

echo "==> firewall: the API is internal-only, never expose 8080"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw --force enable >/dev/null
ufw status verbose | head -6

echo "==> config"
cd "$APP_DIR"
[ -f .env ] || { cp .env.example .env; echo "    WROTE .env FROM TEMPLATE — set WS_API_KEY before starting"; }
[ -f config/identities.txt ] || { cp config/identities.example.txt config/identities.txt; \
  echo "    WROTE identities.txt FROM TEMPLATE — add residential exits before starting"; }

echo
echo "==> next, by hand (deliberately not automated):"
echo "    1. .env            : set WS_API_KEY to a real secret (openssl rand -hex 32)"
echo "    2. identities.txt  : one line per identity. Start with NO proxy and watch"
echo "                         block_rate on /health; add residential exits only if it"
echo "                         climbs above ~15%. Trial a provider before buying:"
echo "                         docker compose run --rm web-search python -m scripts.proxy_trial --queries 100"
echo "    3. WS_MAX_OPEN_CONTEXTS must be >= identity count, or every other request"
echo "       evicts and relaunches a browser (~1.3s)."
echo "    4. docker compose up -d --build && curl -s localhost:8080/health"
