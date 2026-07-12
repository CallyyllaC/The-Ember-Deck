#!/usr/bin/env bash
# Launch the kiosk only after both the compositor socket and local UI respond.
set -euo pipefail

URL="http://127.0.0.1:32500"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
WAYLAND_NAME="${WAYLAND_DISPLAY:-wayland-0}"
WAYLAND_SOCKET="$RUNTIME_DIR/$WAYLAND_NAME"

echo "[browser] waiting for Wayland and local web service..."

for _ in $(seq 1 180); do
    if [ -S "$WAYLAND_SOCKET" ] && \
       curl -fsS --max-time 1 "$URL" >/dev/null 2>&1; then
        echo "[browser] ready: $WAYLAND_SOCKET / $URL"
        exec firefox --kiosk "$URL"
    fi
    sleep 0.5
done

echo "[browser] timed out waiting for desktop or local web service"
exit 1
