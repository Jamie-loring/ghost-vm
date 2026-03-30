# Ghost Browser VM

Ghost is a containerised browser automation platform for covert web operations. It runs a full GUI Chromium browser with human-paced interaction logic, exposed via noVNC (visual), raw VNC, and a REST API.

> **Operational philosophy:** human speed and timing inside the browser, full autonomy outside it. The goal is invisible operation within authenticated sessions without triggering UEBA / behavioural analytics.

---

## Architecture

```
podman container (localhost/ghost-vm:latest)
├── Ubuntu 22.04 base
├── Xvfb :1              — virtual framebuffer (1920×1080×24)
├── Openbox              — lightweight window manager
├── x11vnc        :5900  — raw VNC server
├── noVNC/websockify :6080 — browser-based VNC client
├── Playwright Chromium  — visible/headless browser
│   ├── Broad font stack  (defeats font-enumeration fingerprinting)
│   └── MS core fonts     (Arial, Verdana, Times New Roman, etc.)
└── FastAPI        :8080  — REST automation API
    ├── api.py           — endpoint definitions + Playwright integration
    ├── human.py         — human-paced mouse/keyboard interaction layer
    └── creds.py         — encrypted-at-rest credential store (Fernet)
```

---

## Ports

| Port | Service |
|------|---------|
| `6080` | noVNC web UI → `http://localhost:6080/vnc.html` |
| `5900` | Raw VNC (any VNC client, 8-char password) |
| `8080` | Automation REST API (`X-API-Key` header required) |

---

## Quick Start

### Build

```bash
git clone https://github.com/Jamie-loring/ghost-vm.git
cd ghost-vm
podman build -t ghost-vm .
```

First build downloads Playwright Chromium (~1.5 GB) and MS core fonts. Subsequent builds use layer cache.

### Run

```bash
podman run -d \
  --name ghost-vm \
  -p 5900:5900 -p 6080:6080 -p 8080:8080 \
  -e VNC_PASSWORD="<strong-8-char-password>" \
  -e API_KEY="<random-api-key-min-32-chars>" \
  -e SCREEN_RESOLUTION="1920x1080x24" \
  -e TZ="America/New_York" \
  -v browser-profile:/home/user/.config/chromium \
  -v "$(pwd)/automation":/app \
  --shm-size=512m \
  --cap-add=SYS_ADMIN \
  localhost/ghost-vm:latest
```

Credentials are **never** hardcoded — set them as env vars at runtime and store them externally (e.g. `pass`, a vault, or an env file not committed to git).

---

## browser-profile Volume

The `browser-profile` named volume persists Chromium's full profile across container restarts: cookies, localStorage, IndexedDB, cache, session state. This accumulated age is what makes ghost look human to UEBA/behavioural analytics.

> **Never wipe this volume carelessly.** Session age that took weeks to build cannot be recreated overnight.

**Backup before anything destructive:**
```bash
podman volume export browser-profile > ~/ghost-vm/profile-backup-$(date +%Y%m%d).tar
```

**Restore:**
```bash
podman volume import browser-profile < ~/ghost-vm/profile-backup-YYYYMMDD.tar
```

---

## Rebuild (image only, preserve profile)

```bash
podman stop ghost-vm && podman rm ghost-vm
podman build -t ghost-vm .
# Re-run with the same podman run command above
```

`browser-profile` survives `podman rm` — only `podman volume rm browser-profile` destroys it.

---

## API Reference

All endpoints require `X-API-Key: <your-key>` header. Unauthenticated requests return 403.

### Browser Control

```bash
# Navigate to URL
curl -X POST http://localhost:8080/navigate \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# Screenshot current state
curl http://localhost:8080/screenshot \
  -H "X-API-Key: <key>" --output screen.png

# Get current page URL and title
curl http://localhost:8080/page-info -H "X-API-Key: <key>"
```

### Human-Paced Interaction

```bash
# Type text at human speed
curl -X POST http://localhost:8080/type \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"text": "search query", "wpm": 45}'

# Click element by CSS selector
curl -X POST http://localhost:8080/click \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"selector": "button[type=submit]"}'

# Human scroll
curl -X POST http://localhost:8080/scroll \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"direction": "down", "amount": 300}'
```

### Credential Store

Credentials are encrypted at rest using Fernet symmetric encryption, keyed from the `API_KEY` env var. Only accessible to someone who already has API access.

```bash
# Store credentials for a target application
curl -X POST http://localhost:8080/creds/store \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"service": "target-app", "username": "user@example.com", "password": "...", "totp_secret": "BASE32SECRET"}'

# List stored services (never returns passwords)
curl http://localhost:8080/creds/list -H "X-API-Key: <key>"

# Auto-fill login form from stored creds
curl -X POST http://localhost:8080/login \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"service": "target-app", "username_selector": "#username", "password_selector": "#password"}'

# Get current TOTP code
curl http://localhost:8080/creds/totp/target-app -H "X-API-Key: <key>"
```

---

## noVNC Access

Open `http://localhost:6080/vnc.html` in a browser, enter your VNC password, and watch/interact with the live browser session in real time.

---

## Fingerprint Hardening

Ghost ships with several anti-fingerprinting measures:

* **Broad font stack** — 12+ font families covering system font enumerations
* **MS core fonts** — Arial, Verdana, Georgia, Times New Roman, Courier New (SourceForge, EULA pre-accepted at build time)
* **Realistic Chromium prefs** — `chrome_prefs.json` baked into image, matching common profile defaults
* **`--password-store=basic`** — prevents keychain prompts
* **`--use-mock-keychain`** — suppresses OS-level credential dialogs
* **Non-root user** — avoids `--no-sandbox` flag (a known automation signal)

---

## Dependencies

See `automation/requirements.txt`:
```
playwright, fastapi, uvicorn, human-paced-input (pyautogui, python-xlib),
pydantic, httpx, Pillow, numpy, scipy, pyotp, cryptography
```

---

## Legal & Ethics

Ghost is a personal automation tool. Use only on systems and accounts you own or have explicit authorisation to access. Do not use to violate platform terms of service, circumvent security controls you don't own, or conduct surveillance without consent.

---

**Author:** Jamie Loring
