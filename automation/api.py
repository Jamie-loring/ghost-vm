"""
api.py — REST control interface for the browser VM

Endpoints let external callers drive the browser with human-like behavior.
Browser launches on startup against the Xvfb display (:1).

noVNC:  http://localhost:6080/vnc.html  (visual monitor)
API:    http://localhost:8080           (programmatic control)
"""

import asyncio
import base64
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional, Any

from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from human import UserProfile, HumanMouse, HumanKeyboard, HumanBehavior, _wait_stable_url, _url_domain
from creds import CredentialManager

# ---------------------------------------------------------------------------
# API key auth — set API_KEY env var at container launch
# ---------------------------------------------------------------------------

_API_KEY = os.environ.get("API_KEY", "")
if not _API_KEY:
    raise RuntimeError("API_KEY environment variable must be set")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def require_api_key(key: str = Security(_api_key_header)) -> None:
    if not secrets.compare_digest(key, _API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Browser launch args — tuned for UEBA realism
# ---------------------------------------------------------------------------

CHROMIUM_ARGS = [
    # Window geometry matching the Xvfb screen
    "--window-size=1920,1080",
    "--window-position=0,0",
    "--start-maximized",
    # Suppress automation markers — critical for webdriver flag
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--exclude-switches=enable-automation",
    # Realistic renderer behavior
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-ipc-flooding-protection",
    # Hardware/graphics (Xvfb has software rendering)
    "--disable-gpu",
    "--use-gl=swiftshader",
    # Audio (silence but present)
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    # Misc
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-translate",
    "--disable-default-apps",
    # Password/credential saving UI — don't prompt
    "--password-store=basic",
    "--use-mock-keychain",
]

# Persistent profile dir — passed to launch_persistent_context, not as an arg
USER_DATA_DIR = "/home/user/.config/chromium"

# UA must match the actual playwright chromium version (145) so userAgentData
# and the UA string don't contradict each other — that mismatch was our biggest lie.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1920, "height": 1080}


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

class BrowserState:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        # One profile per session — all behavioral classes share it so that
        # WPM, click speed, etc. are consistent across the whole session.
        profile = UserProfile()
        self.mouse    = HumanMouse(profile)
        self.keyboard = HumanKeyboard(profile)
        self.behavior = HumanBehavior(profile, mouse=self.mouse)
        self.creds    = CredentialManager(_API_KEY)
        # Serializes all browser-driving operations so concurrent API callers
        # (e.g. two sweep scripts running in parallel) can't interleave nav/click
        # calls and end up fighting over the same page.
        self.lock     = asyncio.Lock()


state = BrowserState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    state.playwright = await async_playwright().start()

    # Ubuntu 22.04's chromium-browser is a snap stub and won't run in a container.
    # Use playwright's bundled chromium which was installed at image build time.
    chromium_path = None

    # Use launch_persistent_context so the profile (cookies, history, cache)
    # survives container restarts and looks like a real returning user.
    state.context = await state.playwright.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        headless=False,                      # REAL window on Xvfb — not headless
        executable_path=chromium_path,
        args=CHROMIUM_ARGS,
        env={"DISPLAY": ":1"},
        viewport=VIEWPORT,
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="light",
        device_scale_factor=1,
        has_touch=False,
        is_mobile=False,
        permissions=["geolocation", "notifications"],
    )

    # Ghost stealth patches v2 — applied to every page before any script runs
    await state.context.add_init_script("""
    (() => {
        // ------------------------------------------------------------------
        // 1. WebDriver flag — three-layer removal so it appears absent:
        //    a) delete/shadow on Navigator.prototype
        //    b) proxy window.navigator so `'webdriver' in navigator` → false
        //    c) patch Object.getOwnPropertyDescriptor to hide it
        // ------------------------------------------------------------------
        try {
            const navProto = Object.getPrototypeOf(navigator);
            const desc = Object.getOwnPropertyDescriptor(navProto, 'webdriver');
            if (desc && desc.configurable) {
                delete navProto.webdriver;
            } else if (desc) {
                Object.defineProperty(navProto, 'webdriver', {
                    ...desc, get: () => undefined, configurable: true,
                });
            }
        } catch(_) {}
        try {
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined, configurable: true, enumerable: false,
            });
        } catch(_) {}
        // Proxy navigator so `'webdriver' in navigator` and descriptor checks see nothing
        try {
            const _nav = navigator;
            const navProxy = new Proxy(_nav, {
                has(t, p)  { if (p === 'webdriver') return false; return p in t; },
                get(t, p, r) {
                    if (p === 'webdriver') return undefined;
                    const v = Reflect.get(t, p, t);
                    return typeof v === 'function' ? v.bind(t) : v;
                },
                getOwnPropertyDescriptor(t, p) {
                    if (p === 'webdriver') return undefined;
                    return Object.getOwnPropertyDescriptor(t, p);
                },
            });
            Object.defineProperty(window, 'navigator', {
                get: () => navProxy, configurable: true, enumerable: true,
            });
        } catch(_) {}
        // Hide the descriptor from direct Object.getOwnPropertyDescriptor calls
        try {
            const _goopd = Object.getOwnPropertyDescriptor;
            Object.getOwnPropertyDescriptor = function(obj, prop) {
                if (prop === 'webdriver' &&
                    (obj === navigator || obj === Object.getPrototypeOf(navigator))) {
                    return undefined;
                }
                return _goopd.call(this, obj, prop);
            };
        } catch(_) {}

        // ------------------------------------------------------------------
        // 2. chrome runtime — full object that detection scripts probe
        // ------------------------------------------------------------------
        window.chrome = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed' },
                RunningState: { CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running' },
            },
            csi: () => ({ onloadT: Date.now(), pageT: Date.now() - performance.timing.navigationStart, startE: Date.now() - 1000, tran: 15 }),
            loadTimes: () => ({
                commitLoadTime: (Date.now() - 3000) / 1000,
                connectionInfo: 'h2', finishDocumentLoadTime: (Date.now() - 1500) / 1000,
                finishLoadTime: (Date.now() - 1200) / 1000, firstPaintAfterLoadTime: 0,
                firstPaintTime: (Date.now() - 2000) / 1000, navigationType: 'Other',
                npnNegotiatedProtocol: 'h2', requestTime: (Date.now() - 4000) / 1000,
                startLoadTime: (Date.now() - 3800) / 1000, wasAlternateProtocolAvailable: false,
                wasFetchedViaSpdy: true, wasNpnNegotiated: true,
            }),
            runtime: {
                connect: () => {}, sendMessage: () => {}, id: undefined,
                PlatformOs: { MAC:'mac', WIN:'win', ANDROID:'android', CROS:'cros', LINUX:'linux', OPENBSD:'openbsd' },
                PlatformArch: { ARM:'arm', ARM64:'arm64', X86_32:'x86-32', X86_64:'x86-64', MIPS:'mips', MIPS64:'mips64' },
                RequestUpdateCheckStatus: { THROTTLED:'throttled', NO_UPDATE:'no_update', UPDATE_AVAILABLE:'update_available' },
                OnInstalledReason: { INSTALL:'install', UPDATE:'update', CHROME_UPDATE:'chrome_update', SHARED_MODULE_UPDATE:'shared_module_update' },
                OnRestartRequiredReason: { APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic' },
            },
        };

        // ------------------------------------------------------------------
        // 3. Plugins — proper PluginArray/Plugin prototype chain so that
        //    instanceof checks and type checks pass.
        // ------------------------------------------------------------------
        try {
            const makeMime = (type, suffix, desc) => {
                const m = Object.create(MimeType.prototype);
                Object.defineProperties(m, {
                    type:        { value: type,   enumerable: true },
                    suffixes:    { value: suffix, enumerable: true },
                    description: { value: desc,   enumerable: true },
                });
                return m;
            };
            const makePlugin = (name, filename, desc, mimes) => {
                const p = Object.create(Plugin.prototype);
                Object.defineProperties(p, {
                    name:        { value: name,         enumerable: true },
                    filename:    { value: filename,     enumerable: true },
                    description: { value: desc,         enumerable: true },
                    length:      { value: mimes.length, enumerable: true },
                });
                mimes.forEach((m, i) => { p[i] = m; });
                return p;
            };
            const pdfMime = makeMime('application/pdf', 'pdf', 'Portable Document Format');
            const plugins = [
                makePlugin('PDF Viewer',       'mhjfbmdgcfjbbpaeojofohoefgiehjai', 'Portable Document Format', [pdfMime]),
                makePlugin('Chrome PDF Viewer','internal-pdf-viewer',              'Portable Document Format', [pdfMime]),
                makePlugin('Chromium PDF Plugin','internal-pdf-viewer',            'Portable Document Format', [pdfMime]),
            ];
            const pArr = Object.create(PluginArray.prototype);
            plugins.forEach((p, i) => { pArr[i] = p; });
            Object.defineProperty(pArr, 'length', { value: plugins.length });
            Object.defineProperty(navigator, 'plugins', { get: () => pArr, configurable: true });

            const mArr = Object.create(MimeTypeArray.prototype);
            [pdfMime].forEach((m, i) => { mArr[i] = m; });
            Object.defineProperty(mArr, 'length', { value: 1 });
            Object.defineProperty(navigator, 'mimeTypes', { get: () => mArr, configurable: true });
        } catch(_) {}

        // ------------------------------------------------------------------
        // 4. WebGL GPU spoof — replace SwiftShader with a common Intel GPU
        //    Patched on both WebGLRenderingContext and WebGL2RenderingContext.
        // ------------------------------------------------------------------
        (() => {
            const VENDOR   = 'Intel Inc.';
            const RENDERER = 'Intel(R) Iris(R) Xe Graphics';
            const UNMASKED_VENDOR_WEBGL   = 37445;
            const UNMASKED_RENDERER_WEBGL = 37446;
            const patchProto = (proto) => {
                if (!proto) return;
                const orig = proto.getParameter;
                proto.getParameter = function(p) {
                    if (p === UNMASKED_VENDOR_WEBGL)   return VENDOR;
                    if (p === UNMASKED_RENDERER_WEBGL) return RENDERER;
                    return orig.call(this, p);
                };
            };
            if (typeof WebGLRenderingContext  !== 'undefined') patchProto(WebGLRenderingContext.prototype);
            if (typeof WebGL2RenderingContext !== 'undefined') patchProto(WebGL2RenderingContext.prototype);
        })();

        // ------------------------------------------------------------------
        // 5. userAgentData — keep version consistent with UA string (145)
        // ------------------------------------------------------------------
        (() => {
            const brands = [
                { brand: 'Not)A;Brand',  version: '99'  },
                { brand: 'Google Chrome', version: '145' },
                { brand: 'Chromium',      version: '145' },
            ];
            const fullList = [
                { brand: 'Not)A;Brand',  version: '99.0.0.0'   },
                { brand: 'Google Chrome', version: '145.0.7632.6' },
                { brand: 'Chromium',      version: '145.0.7632.6' },
            ];
            const uad = {
                brands, mobile: false, platform: 'Linux',
                getHighEntropyValues: async (hints) => {
                    const all = {
                        architecture: 'x86', bitness: '64', brands, fullVersionList: fullList,
                        mobile: false, model: '', platform: 'Linux', platformVersion: '5.15.0',
                        uaFullVersion: '145.0.7632.6', wow64: false,
                    };
                    return Object.fromEntries(hints.filter(h => h in all).map(h => [h, all[h]]));
                },
                toJSON: () => ({ brands, mobile: false, platform: 'Linux' }),
            };
            try { Object.defineProperty(navigator, 'userAgentData', { get: () => uad, configurable: true }); } catch(_) {}
        })();

        // ------------------------------------------------------------------
        // 6. Misc navigator / screen consistency patches
        // ------------------------------------------------------------------
        try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true }); } catch(_) {}
        // Do NOT override hardwareConcurrency — worker reports the real value;
        // any mismatch here is itself a detection signal.

        const sd = (obj, k, v) => { try { Object.defineProperty(obj, k, { get: () => v, configurable: true }); } catch(_) {} };
        sd(screen, 'width',       1920);
        sd(screen, 'height',      1080);
        sd(screen, 'availWidth',  1920);
        sd(screen, 'availHeight', 1040);
        sd(screen, 'colorDepth',  24);
        sd(screen, 'pixelDepth',  24);

        // ------------------------------------------------------------------
        // 7. Permissions API
        // ------------------------------------------------------------------
        try {
            const origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(p);
        } catch(_) {}

        // ------------------------------------------------------------------
        // 7b. Media devices — replace fake Chromium device labels with
        //     realistic hardware names while keeping real deviceIds/groupIds.
        // ------------------------------------------------------------------
        (() => {
            if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
            const _enum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
            const AUDIO_IN  = ['Built-in Microphone', 'Microphone (Realtek(R) Audio)'];
            const AUDIO_OUT = ['Speakers (Realtek(R) Audio)', 'Built-in Audio Analog Stereo'];
            const VIDEO_IN  = ['Integrated Webcam'];
            navigator.mediaDevices.enumerateDevices = async function() {
                const devs = await _enum();
                let ai = 0, ao = 0, vi = 0;
                return devs.map(d => {
                    let label = d.label;
                    if (label && label.toLowerCase().includes('fake')) {
                        if (d.kind === 'audioinput')  label = AUDIO_IN[ai++]  || 'Microphone';
                        if (d.kind === 'audiooutput') label = AUDIO_OUT[ao++] || 'Speakers';
                        if (d.kind === 'videoinput')  label = VIDEO_IN[vi++]  || 'Webcam';
                    }
                    try {
                        const out = Object.create(MediaDeviceInfo.prototype);
                        Object.defineProperties(out, {
                            deviceId: { value: d.deviceId, enumerable: true },
                            groupId:  { value: d.groupId,  enumerable: true },
                            kind:     { value: d.kind,     enumerable: true },
                            label:    { value: label,      enumerable: true },
                            toJSON:   { value: () => ({ deviceId: d.deviceId, groupId: d.groupId, kind: d.kind, label }) },
                        });
                        return out;
                    } catch(_) {
                        return { deviceId: d.deviceId, groupId: d.groupId, kind: d.kind, label };
                    }
                });
            };
        })();

        // ------------------------------------------------------------------
        // 8. Broken image dimensions — return 0x0 (real Chrome behaviour)
        // ------------------------------------------------------------------
        try {
            ['naturalWidth', 'naturalHeight'].forEach(prop => {
                const orig = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, prop);
                if (!orig) return;
                Object.defineProperty(HTMLImageElement.prototype, prop, {
                    get: function() {
                        if (this.complete && (this.src === '' || this.src === window.location.href)) return 0;
                        return orig.get.call(this);
                    },
                    configurable: true,
                });
            });
        } catch(_) {}

        // ------------------------------------------------------------------
        // 9. Speech synthesis — populate a realistic Google voice list so
        //    getVoices() doesn't return empty (a strong headless signal).
        // ------------------------------------------------------------------
        (() => {
            if (!window.speechSynthesis) return;
            const VOICES = [
                ['Google US English',        'en-US', false, true ],
                ['Google UK English Female', 'en-GB', false, false],
                ['Google UK English Male',   'en-GB', false, false],
                ['Google Deutsch',           'de-DE', false, false],
                ['Google español',           'es-ES', false, false],
                ['Google français',          'fr-FR', false, false],
                ['Google italiano',          'it-IT', false, false],
                ['Google 日本語',             'ja-JP', false, false],
                ['Google 한국의',             'ko-KR', false, false],
                ['Google 中文（简体）',       'zh-CN', false, false],
            ];
            const makeVoice = ([name, lang, localService, dflt]) => {
                try {
                    const v = Object.create(SpeechSynthesisVoice.prototype);
                    Object.defineProperties(v, {
                        name:         { value: name,         enumerable: true },
                        lang:         { value: lang,         enumerable: true },
                        localService: { value: localService, enumerable: true },
                        default:      { value: dflt,         enumerable: true },
                        voiceURI:     { value: name,         enumerable: true },
                    });
                    return v;
                } catch(_) {
                    return { name, lang, localService, default: dflt, voiceURI: name };
                }
            };
            const voices = VOICES.map(makeVoice);
            window.speechSynthesis.getVoices = () => voices;
            setTimeout(() => window.dispatchEvent(new Event('voiceschanged')), 50);
        })();

    })();
    """)

    state.page = await state.context.new_page()
    await state.page.goto("about:blank")

    # Track popup windows — when a new page opens (e.g. Google OAuth popup),
    # automatically make it the active page so API calls land there.
    def _on_new_page(page):
        print(f"[api] new page opened: {page.url}")
        state.page = page
        state.mouse._pos = (960.0, 540.0)  # reset mouse position for new window

    state.context.on("page", _on_new_page)

    print("[api] browser ready — connect at http://localhost:6080/vnc.html")
    yield

    # ---- shutdown ----
    if state.context:
        await state.context.close()
    if state.playwright:
        await state.playwright.stop()


app = FastAPI(
    title="Browser VM Control API",
    lifespan=lifespan,
    dependencies=[Security(require_api_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class NavigateReq(BaseModel):
    url: str
    human: bool = True          # use natural_navigate (think + scan)
    wait_load: bool = True

class ClickReq(BaseModel):
    selector: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    button: str = "left"
    speed_factor: float = 1.0

class TypeReq(BaseModel):
    selector: Optional[str] = None   # focus element first if given
    text: str
    wpm: Optional[float] = None
    clear_first: bool = False

class ScrollReq(BaseModel):
    delta_y: int                # positive = down
    x: Optional[float] = None
    y: Optional[float] = None

class EvalReq(BaseModel):
    expression: str             # JS to evaluate in page context

class ThinkReq(BaseModel):
    min_ms: int = 400
    max_ms: int = 2500

class ReadPageReq(BaseModel):
    word_count: Optional[int] = None

class IdleReq(BaseModel):
    duration_s: float = 10.0

class StoreCredReq(BaseModel):
    service: str                        # arbitrary name, e.g. "github", "twitter"
    username: str                       # email or username
    password: str
    totp_secret: Optional[str] = None  # base32 TOTP seed (e.g. from authenticator app QR)
    notes: str = ""

class LoginReq(BaseModel):
    service: str                        # must match a stored service name
    url: Optional[str] = None          # navigate here first if supplied
    # CSS selectors — defaults cover most sites; override for awkward login pages
    username_selector: str = (
        'input[type="email"], input[type="text"][name*="user"], '
        'input[type="text"][name*="email"], input[type="text"][id*="user"], '
        'input[type="text"][id*="email"], input[autocomplete="username"], '
        'input[autocomplete="email"]'
    )
    password_selector: str = 'input[type="password"]'
    submit_selector: Optional[str] = None   # if None, presses Enter after password
    totp_selector: Optional[str] = None     # selector for the 2FA code input field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page() -> Page:
    if state.page is None:
        raise HTTPException(status_code=503, detail="Browser not ready")
    return state.page


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/status")
async def status():
    """Health check — returns current URL."""
    page = _page()
    return {"status": "ok", "url": page.url}


@app.post("/navigate")
async def navigate(req: NavigateReq):
    async with state.lock:
        page = _page()
        if req.human:
            result = await state.behavior.natural_navigate(page, req.url, wait_for_load=req.wait_load)
        else:
            await page.goto(req.url, wait_until="domcontentloaded" if req.wait_load else "commit")
            final_url = await _wait_stable_url(page)
            requested_domain = _url_domain(req.url)
            final_domain = _url_domain(final_url)
            stable = bool(
                requested_domain and final_domain and
                (requested_domain == final_domain or
                 requested_domain.endswith("." + final_domain) or
                 final_domain.endswith("." + requested_domain))
            )
            result = {"requested": req.url, "url": final_url, "stable": stable}
        if not result["stable"]:
            print(f"[nav] UNSTABLE: requested={result['requested']} landed={result['url']}")
        return result


@app.post("/click")
async def click(req: ClickReq):
    async with state.lock:
        page = _page()
        if req.selector:
            await state.mouse.click_element(page, req.selector, button=req.button, speed_factor=req.speed_factor)
        elif req.x is not None and req.y is not None:
            await state.mouse.click(page, req.x, req.y, button=req.button, speed_factor=req.speed_factor)
        else:
            raise HTTPException(status_code=400, detail="Provide selector or x,y coordinates")
        return {"ok": True}


@app.post("/type")
async def type_text(req: TypeReq):
    async with state.lock:
        page = _page()
        if req.selector:
            await state.mouse.click_element(page, req.selector)
            await asyncio.sleep(0.15)
        await state.keyboard.type(page, req.text, wpm=req.wpm, clear_first=req.clear_first)
        return {"ok": True, "chars": len(req.text)}


@app.post("/scroll")
async def scroll(req: ScrollReq):
    async with state.lock:
        page = _page()
        await state.mouse.scroll(page, req.delta_y, x=req.x, y=req.y)
        return {"ok": True}


@app.post("/think")
async def think(req: ThinkReq):
    async with state.lock:
        await state.behavior.think(req.min_ms, req.max_ms, page=state.page)
        return {"ok": True}


@app.post("/read_page")
async def read_page(req: ReadPageReq):
    async with state.lock:
        page = _page()
        await state.behavior.read_page(page, word_count=req.word_count)
        return {"ok": True}


@app.post("/idle")
async def idle(req: IdleReq):
    async with state.lock:
        page = _page()
        await state.behavior.idle(page, req.duration_s)
        return {"ok": True}


@app.post("/key")
async def key(key: str):
    async with state.lock:
        page = _page()
        await state.keyboard.press(page, key)
        return {"ok": True}


@app.post("/screenshot")
async def screenshot():
    """Returns a base64-encoded PNG of the current viewport."""
    page = _page()
    data = await page.screenshot(type="png")
    return {"image": base64.b64encode(data).decode()}


@app.post("/eval")
async def evaluate(req: EvalReq):
    """Run arbitrary JS in the page. Use carefully."""
    async with state.lock:
        page = _page()
        try:
            result = await page.evaluate(req.expression)
            return {"result": result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/page_info")
async def page_info():
    """Return title, URL, and viewport dimensions."""
    page = _page()
    title = await page.title()
    viewport = page.viewport_size
    return {"url": page.url, "title": title, "viewport": viewport}


@app.post("/new_tab")
async def new_tab(url: str = "about:blank"):
    async with state.lock:
        state.page = await state.context.new_page()
        if url != "about:blank":
            await state.page.goto(url)
        return {"ok": True, "url": state.page.url}


@app.get("/pages")
async def list_pages():
    """List all open pages/popups with their URLs."""
    pages = state.context.pages
    current_url = state.page.url if state.page else None
    return {
        "count": len(pages),
        "current": current_url,
        "pages": [{"index": i, "url": p.url} for i, p in enumerate(pages)],
    }


@app.post("/switch_page")
async def switch_page(index: int):
    """Switch active page to the given index from /pages."""
    pages = state.context.pages
    if index < 0 or index >= len(pages):
        raise HTTPException(status_code=400, detail=f"Index {index} out of range (0-{len(pages)-1})")
    async with state.lock:
        state.page = pages[index]
        state.mouse._pos = (960.0, 540.0)
    return {"ok": True, "url": state.page.url}


@app.post("/close_tab")
async def close_tab():
    async with state.lock:
        if state.page:
            await state.page.close()
            pages = state.context.pages
            state.page = pages[-1] if pages else await state.context.new_page()
        return {"ok": True}


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

@app.post("/creds/store")
async def creds_store(req: StoreCredReq):
    """Store or overwrite credentials for a named service."""
    try:
        state.creds.store(
            req.service, req.username, req.password,
            req.totp_secret, req.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "service": req.service}


@app.get("/creds/list")
async def creds_list():
    """List stored services. Never returns passwords."""
    return {"services": state.creds.list_services()}


@app.delete("/creds/{service}")
async def creds_delete(service: str):
    """Remove credentials for a service."""
    if not state.creds.delete(service):
        raise HTTPException(status_code=404, detail=f"No credentials for '{service}'")
    return {"ok": True}


@app.get("/creds/totp/{service}")
async def creds_totp(service: str):
    """Return the current 6-digit TOTP code for a service."""
    code = state.creds.get_totp(service)
    if code is None:
        raise HTTPException(status_code=404, detail=f"No TOTP secret for '{service}'")
    return {"service": service, "code": code}


@app.post("/creds/login")
async def creds_login(req: LoginReq):
    """
    Authenticate on a site using stored credentials.
    Drives the browser with human typing and timing throughout.
    Optionally handles a 2FA step if totp_selector is supplied.
    """
    page = _page()
    entry = state.creds.get(req.service)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No credentials stored for '{req.service}'")

    # Navigate first if a URL was provided
    if req.url:
        await state.behavior.natural_navigate(page, req.url)

    # --- Username ---
    try:
        await state.mouse.click_element(page, req.username_selector)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Username field not found: {e}")
    await state.behavior.think(200, 600, page=page)
    await state.keyboard.type(page, entry["username"])
    await state.behavior.think(300, 900, page=page)

    # --- Password ---
    try:
        await state.mouse.click_element(page, req.password_selector)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Password field not found: {e}")
    await state.behavior.think(150, 500, page=page)
    await state.keyboard.type(page, entry["password"])
    await state.behavior.think(300, 800, page=page)

    # --- Submit ---
    if req.submit_selector:
        await state.mouse.click_element(page, req.submit_selector)
    else:
        await state.keyboard.press(page, "Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    # --- 2FA (optional) ---
    if req.totp_selector:
        secret = entry.get("totp_secret")
        if not secret:
            raise HTTPException(status_code=400, detail=f"No TOTP secret stored for '{req.service}'")
        import pyotp
        await state.behavior.think(600, 1800, page=page)
        code = pyotp.TOTP(secret).now()
        try:
            await state.mouse.click_element(page, req.totp_selector)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"2FA field not found: {e}")
        await state.behavior.think(200, 500, page=page)
        await state.keyboard.type(page, code)
        await state.behavior.think(200, 600, page=page)
        await state.keyboard.press(page, "Enter")
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

    return {"ok": True, "url": page.url}
