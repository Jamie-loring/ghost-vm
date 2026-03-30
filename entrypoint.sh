#!/bin/bash
set -e

VNC_PASSWORD="${VNC_PASSWORD:-}"
VNC_PORT=5900
NOVNC_PORT=6080
API_PORT=8080

cleanup() {
    echo "[entrypoint] shutting down..."
    kill $(jobs -p) 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Clear stale X11 sockets/locks (survive container stop/start) ---
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# --- Virtual framebuffer ---
echo "[entrypoint] starting Xvfb at $SCREEN_RESOLUTION on :1"
Xvfb :1 -screen 0 "$SCREEN_RESOLUTION" -ac -nolisten tcp +extension RANDR &
sleep 1

export DISPLAY=:1

# --- Window manager ---
echo "[entrypoint] starting openbox"
su -c "DISPLAY=:1 openbox --config-file /dev/null &" user
sleep 0.5

# --- x11vnc ---
echo "[entrypoint] starting x11vnc on :$VNC_PORT"
if [ -n "$VNC_PASSWORD" ]; then
    x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass
    x11vnc -display :1 -rfbauth /tmp/vncpass \
        -rfbport "$VNC_PORT" -listen 0.0.0.0 -forever -shared -noxdamage -xkb &
else
    x11vnc -display :1 -nopw -rfbport "$VNC_PORT" -listen 0.0.0.0 -forever -shared -noxdamage -xkb &
fi
sleep 0.5

# --- noVNC websocket bridge ---
echo "[entrypoint] starting noVNC on :$NOVNC_PORT"
websockify --web /usr/share/novnc/ "$NOVNC_PORT" "localhost:$VNC_PORT" &
sleep 0.5

# --- Clear stale Chromium profile locks (survive container restarts) ---
rm -f /home/user/.config/chromium/SingletonLock \
      /home/user/.config/chromium/SingletonSocket \
      /home/user/.config/chromium/SingletonCookie

# --- Automation API ---
echo "[entrypoint] starting automation API on :$API_PORT"
exec su -c "cd /app && python3 -m uvicorn api:app --host 0.0.0.0 --port $API_PORT --reload" user
