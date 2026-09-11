# Real Google Chrome + a virtual display.
#
# The virtual display is not a convenience — it is the whole trick. Headless Chrome
# is a detection CLASS, not a flag: it reports different screen metrics, different
# WebGL behaviour, and puts "HeadlessChrome" in its own UA. Running headful under
# Xvfb removes that entire class, because Chrome genuinely has a display; it just
# happens not to be attached to a monitor.
#
# Bundled Chromium has its own tells, so we install Google Chrome Stable and point
# Playwright at it with channel="chrome".

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99

# xvfb-run shells out to `xauth` to create the display cookie. Without it the
# container boot-loops on "xvfb-run: error: xauth command not found" — and note
# the comment lives HERE, not inside the RUN: a `#` inside a line continuation
# comments out the rest of the package list.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation fonts-noto-color-emoji \
        xvfb xauth x11-utils dbus-x11 \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
        libpango-1.0-0 libcairo2 \
    && wget -qO /usr/share/keyrings/google-chrome.asc \
        https://dl.google.com/linux/linux_signing_key.pub \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.asc] \
        http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
# scripts/ ships too: proxy_trial is how you decide whether a proxy provider is
# worth buying, and it has to run from the box whose IP is being measured.
COPY scripts ./scripts

# Chrome profiles are production state: they age into looking like real browsers.
# Mount a volume here — losing it resets every identity to cold and suspicious.
RUN mkdir -p /var/lib/open-web-search/profiles
VOLUME ["/var/lib/open-web-search"]

EXPOSE 8080

# The entrypoint brings up Xvfb, waits for it to accept connections, then execs
# the app so uvicorn is PID 1 — logs reach docker and `docker stop` works.
# (`xvfb-run` was tried first and silently never started the child.)
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
