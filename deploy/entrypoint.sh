#!/bin/sh
# Start the virtual display, then hand PID 1 to the app.
#
# This replaces `xvfb-run`, which silently failed here: Xvfb came up but the child
# command never ran and produced no output at all, so the container sat "Up" and
# healthy-looking with nothing listening. `xvfb-run` also wraps the process, which
# swallows logs and breaks signal handling — `exec` below makes uvicorn PID 1, so
# docker sees its output and `docker stop` actually reaches it.

set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN="${XVFB_SCREEN:-1920x1080x24}"
export DISPLAY=":${DISPLAY_NUM}"

echo "[entrypoint] starting Xvfb on ${DISPLAY} (${SCREEN})"
Xvfb "${DISPLAY}" -screen 0 "${SCREEN}" -nolisten tcp &
XVFB_PID=$!

# Wait for the display to actually accept connections. Chrome launched against a
# half-started X server fails in ways that look like anti-bot blocks.
i=0
while [ "$i" -lt 50 ]; do
    if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        echo "[entrypoint] Xvfb ready after $((i * 100))ms (pid ${XVFB_PID})"
        break
    fi
    i=$((i + 1))
    sleep 0.1
done

if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: Xvfb did not come up on ${DISPLAY}" >&2
    exit 1
fi

exec "$@"
