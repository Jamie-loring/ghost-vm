"""
human.py — UEBA-realistic browser behavior simulation

Provides human-like mouse movement, typing, scrolling, and timing
patterns that defeat behavioral analytics checks.
"""

import asyncio
import random
import math
import time
from typing import Tuple, List, Optional
from urllib.parse import urlparse

import numpy as np
from playwright.async_api import Page, Locator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _wait_stable_url(page: Page, timeout_ms: int = 3000, settle_ms: int = 500) -> str:
    """
    Poll page.url until it stops changing for `settle_ms` milliseconds.
    Returns the final stable URL. Catches JS redirects that fire after
    domcontentloaded (e.g. bot-detection bounces, SPA router redirects).
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_url = page.url
    last_change = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        current = page.url
        if current != last_url:
            last_url = current
            last_change = asyncio.get_event_loop().time()
        elif asyncio.get_event_loop().time() - last_change >= settle_ms / 1000:
            return last_url
    return page.url


def _url_domain(url: str) -> str:
    """Return netloc of a URL, empty string on parse failure."""
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _gauss_clamp(mean: float, std: float, lo: float, hi: float) -> float:
    return float(np.clip(np.random.normal(mean, std), lo, hi))


def _bezier_cubic(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
    n: int = 60,
) -> List[Tuple[float, float]]:
    """Cubic Bezier from p0 to p3 through control points p1, p2."""
    points = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3*mt**2*t * p1[0] + 3*mt*t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3*mt**2*t * p1[1] + 3*mt*t**2 * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def _build_mouse_path(
    start: Tuple[float, float],
    end: Tuple[float, float],
    jitter: float = 2.0,
    overshoot: bool = True,
) -> List[Tuple[float, float]]:
    """
    Generate a natural-looking mouse path from start to end.
    Uses cubic Bezier with randomized control points and optional overshoot.
    """
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)
    if dist < 2:
        return [end]

    # Perpendicular offset for control points (gives the curve its arc)
    mid_x = (sx + ex) / 2
    mid_y = (sy + ey) / 2
    perp_x = -(ey - sy) / dist
    perp_y = (ex - sx) / dist
    arc_offset = _gauss_clamp(0, dist * 0.15, -dist * 0.4, dist * 0.4)

    cp1 = (
        sx + (ex - sx) * 0.25 + perp_x * arc_offset + random.uniform(-jitter, jitter),
        sy + (ey - sy) * 0.25 + perp_y * arc_offset + random.uniform(-jitter, jitter),
    )
    cp2 = (
        sx + (ex - sx) * 0.75 + perp_x * arc_offset * 0.5 + random.uniform(-jitter, jitter),
        sy + (ey - sy) * 0.75 + perp_y * arc_offset * 0.5 + random.uniform(-jitter, jitter),
    )

    # Points along the main path
    n_steps = max(20, min(120, int(dist / 5)))
    path = _bezier_cubic(start, cp1, cp2, end, n=n_steps)

    # Occasional overshoot + correction (very human)
    if overshoot and dist > 80 and random.random() < 0.3:
        over_amount = random.uniform(3, 12)
        over_dir = (
            (ex - sx) / dist * over_amount,
            (ey - sy) / dist * over_amount,
        )
        overshot = (ex + over_dir[0], ey + over_dir[1])
        path += _bezier_cubic(overshot, overshot, end, end, n=8)

    return path


def _path_delays(path: List[Tuple], base_speed_px_per_s: float = 1200) -> List[float]:
    """
    Compute per-step delay (seconds) with natural acceleration/deceleration.
    Faster in the middle, slower at start/end (like real cursor movement).
    """
    n = len(path)
    delays = []
    for i in range(1, n):
        p0 = path[i - 1]
        p1 = path[i]
        dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        # ease-in-out: slow at ends, fast in middle
        t = i / n
        ease = 4 * t * (1 - t)  # parabola peaked at 0.5
        speed = base_speed_px_per_s * (0.3 + 0.7 * ease)
        speed *= _gauss_clamp(1.0, 0.08, 0.75, 1.3)  # per-step jitter
        delay = dist / speed if speed > 0 else 0.001
        delays.append(max(delay, 0.0005))
    return delays


# ---------------------------------------------------------------------------
# Session profile — sampled once so UEBA sees a consistent behavioral baseline
# ---------------------------------------------------------------------------

class UserProfile:
    """
    All per-session behavioral constants live here. Instantiate once at session
    start and pass to HumanMouse / HumanKeyboard / HumanBehavior so that WPM,
    click speed, think cadence, etc. are stable across the whole session.
    UEBA detects intra-session variance; re-randomising every action is a tell.
    """
    def __init__(self):
        self.wpm             = _gauss_clamp(62.0,  14.0,  25.0, 110.0)
        self.click_speed     = _gauss_clamp(1.0,   0.15,   0.6,   1.6)  # speed_factor multiplier
        self.think_scale     = _gauss_clamp(1.0,   0.25,   0.4,   2.0)  # multiplier on all pauses
        self.reading_wpm     = _gauss_clamp(238.0, 45.0,  120.0, 380.0)
        self.typo_rate       = _gauss_clamp(0.015, 0.006, 0.003,  0.045)
        self.micro_move_prob = _gauss_clamp(0.65,  0.15,   0.3,   0.9)  # fidgetiness during pauses


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HumanMouse:
    """
    Drives the Playwright mouse with human-like trajectories.
    Current position is tracked to avoid teleporting.
    """

    def __init__(self, profile: Optional['UserProfile'] = None):
        self._pos: Tuple[float, float] = (960.0, 540.0)
        self._profile = profile

    async def move(
        self,
        page: Page,
        x: float,
        y: float,
        speed_factor: float = 1.0,
    ) -> None:
        """Move to (x, y) along a natural curved path."""
        # Add tiny target jitter (sub-pixel variation in landing spot)
        tx = x + random.uniform(-1.5, 1.5)
        ty = y + random.uniform(-1.5, 1.5)

        path = _build_mouse_path(self._pos, (tx, ty))
        delays = _path_delays(path, base_speed_px_per_s=1100 / speed_factor)

        for i, (px, py) in enumerate(path[1:], start=1):
            await page.mouse.move(px, py)
            await asyncio.sleep(delays[i - 1])

        self._pos = (tx, ty)

    async def click(
        self,
        page: Page,
        x: float,
        y: float,
        button: str = "left",
        speed_factor: Optional[float] = None,
    ) -> None:
        """Move to target then click with realistic mousedown→mouseup timing."""
        if speed_factor is None:
            speed_factor = self._profile.click_speed if self._profile else 1.0
        await self.move(page, x, y, speed_factor=speed_factor)
        # Small pre-click pause (hand settling)
        await asyncio.sleep(_gauss_clamp(0.08, 0.04, 0.02, 0.25))
        hold_ms = _gauss_clamp(90, 30, 40, 250)
        await page.mouse.down(button=button)
        await asyncio.sleep(hold_ms / 1000)
        await page.mouse.up(button=button)
        # Post-click micro-pause
        await asyncio.sleep(_gauss_clamp(0.05, 0.02, 0.01, 0.15))

    async def click_element(
        self,
        page: Page,
        selector: str,
        button: str = "left",
        speed_factor: float = 1.0,
    ) -> None:
        """Locate an element, move to a random point inside its bounding box, click."""
        elem = page.locator(selector).first
        box = await elem.bounding_box()
        if box is None:
            raise ValueError(f"Element not visible: {selector}")
        # Aim at a random spot inside the element (not always dead-center)
        tx = box["x"] + box["width"] * _gauss_clamp(0.5, 0.15, 0.1, 0.9)
        ty = box["y"] + box["height"] * _gauss_clamp(0.5, 0.15, 0.1, 0.9)
        await self.click(page, tx, ty, button=button, speed_factor=speed_factor)

    async def double_click(self, page: Page, x: float, y: float) -> None:
        await self.click(page, x, y)
        interval = _gauss_clamp(0.12, 0.04, 0.06, 0.25)
        await asyncio.sleep(interval)
        hold_ms = _gauss_clamp(80, 25, 40, 180)
        await page.mouse.down()
        await asyncio.sleep(hold_ms / 1000)
        await page.mouse.up()

    async def scroll(
        self,
        page: Page,
        delta_y: int,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> None:
        """
        Scroll with human-like chunking: big scrolls are broken into
        multiple wheel events with variable speed.
        """
        if x is None:
            x = self._pos[0]
        if y is None:
            y = self._pos[1]

        remaining = delta_y
        direction = 1 if delta_y > 0 else -1

        while abs(remaining) > 0:
            chunk = min(abs(remaining), int(_gauss_clamp(120, 40, 40, 240)))
            await page.mouse.wheel(0, direction * chunk)
            remaining -= direction * chunk
            await asyncio.sleep(_gauss_clamp(0.08, 0.03, 0.02, 0.2))


class HumanKeyboard:
    """
    Types text character by character with realistic WPM, typos,
    and self-correction behavior.
    """

    # Common adjacent-key typo pairs (qwerty)
    TYPO_NEIGHBORS = {
        'a': 'sq', 'b': 'vn', 'c': 'xv', 'd': 'sf', 'e': 'wr',
        'f': 'dg', 'g': 'fh', 'h': 'gj', 'i': 'uo', 'j': 'hk',
        'k': 'jl', 'l': 'k;', 'm': 'n,', 'n': 'bm', 'o': 'ip',
        'p': 'o[', 'q': 'aw', 'r': 'et', 's': 'aw', 't': 'ry',
        'u': 'yi', 'v': 'cb', 'w': 'qe', 'x': 'zc', 'y': 'tu',
        'z': 'ax',
    }

    def __init__(self, profile: Optional['UserProfile'] = None):
        self._profile = profile

    def _char_delay(self, wpm: float) -> float:
        """Seconds between keystrokes at the given WPM."""
        chars_per_min = wpm * 5  # standard: 1 word = 5 chars
        base = 60.0 / chars_per_min
        # Add per-keystroke variance (fatigue, hesitation)
        return base * _gauss_clamp(1.0, 0.2, 0.4, 2.5)

    def _should_typo(self, char: str) -> bool:
        rate = self._profile.typo_rate if self._profile else 0.015
        return char.lower() in self.TYPO_NEIGHBORS and random.random() < rate

    async def type(
        self,
        page: Page,
        text: str,
        wpm: Optional[float] = None,
        clear_first: bool = False,
    ) -> None:
        """Type text with human-like pacing and occasional typo+correction."""
        if wpm is None:
            base = self._profile.wpm if self._profile else 62.0
            wpm = _gauss_clamp(base, base * 0.08, base * 0.4, base * 1.8)

        if clear_first:
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.05)
            await page.keyboard.press("Delete")
            await asyncio.sleep(_gauss_clamp(0.12, 0.04, 0.05, 0.3))

        i = 0
        while i < len(text):
            char = text[i]

            # Occasional burst speed variation (typing rhythm)
            burst_wpm = wpm * _gauss_clamp(1.0, 0.1, 0.7, 1.4)

            if self._should_typo(char):
                # Type wrong char
                neighbors = self.TYPO_NEIGHBORS[char.lower()]
                wrong = random.choice(neighbors)
                if char.isupper():
                    wrong = wrong.upper()
                await page.keyboard.type(wrong)
                await asyncio.sleep(self._char_delay(burst_wpm))
                # Pause as if noticing mistake
                await asyncio.sleep(_gauss_clamp(0.25, 0.1, 0.1, 0.6))
                # Backspace
                await page.keyboard.press("Backspace")
                await asyncio.sleep(_gauss_clamp(0.08, 0.03, 0.03, 0.2))
                # Type correct char
                await page.keyboard.type(char)
            else:
                await page.keyboard.type(char)

            await asyncio.sleep(self._char_delay(burst_wpm))

            # Occasional brief pause mid-sentence (thinking, distraction)
            if char in (' ', ',', '.') and random.random() < 0.04:
                await asyncio.sleep(_gauss_clamp(0.8, 0.4, 0.3, 2.5))

            i += 1

    async def press(self, page: Page, key: str, delay_after: float = 0.05) -> None:
        await asyncio.sleep(_gauss_clamp(0.05, 0.02, 0.01, 0.15))
        await page.keyboard.press(key)
        await asyncio.sleep(delay_after)

    async def shortcut(self, page: Page, *keys: str) -> None:
        """Press a key combination (e.g. shortcut(page, 'Control', 'a'))."""
        for key in keys[:-1]:
            await page.keyboard.down(key)
        await page.keyboard.press(keys[-1])
        for key in reversed(keys[:-1]):
            await page.keyboard.up(key)
        await asyncio.sleep(0.05)


class HumanBehavior:
    """
    High-level patterns: reading time, random idle, session pacing.
    """

    def __init__(
        self,
        profile: Optional[UserProfile] = None,
        mouse: Optional[HumanMouse] = None,
    ):
        self.profile = profile or UserProfile()
        # Share the caller's HumanMouse so position state is consistent
        # across the whole session rather than teleporting on each behavior call.
        self._mouse = mouse or HumanMouse(self.profile)

    async def think(
        self,
        min_ms: int = 400,
        max_ms: int = 2500,
        page: Optional[Page] = None,
    ) -> None:
        """
        Random pause simulating thought/decision.
        If page is supplied, adds micro-saccades (tiny cursor drifts) during
        the pause — a stationary cursor during a 'reading' window is a primary
        bot signal in UEBA systems.
        """
        duration = random.uniform(min_ms / 1000, max_ms / 1000) * self.profile.think_scale

        if page is None or duration < 0.4:
            await asyncio.sleep(duration)
            return

        end = asyncio.get_event_loop().time() + duration
        while True:
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            # Wait a short interval, then maybe twitch
            interval = min(_gauss_clamp(0.7, 0.35, 0.25, 1.8), remaining)
            await asyncio.sleep(interval)
            if asyncio.get_event_loop().time() >= end:
                break
            if random.random() < self.profile.micro_move_prob:
                cx, cy = self._mouse._pos
                nx = float(np.clip(_gauss_clamp(cx, 5.0, cx - 18, cx + 18), 50, 1870))
                ny = float(np.clip(_gauss_clamp(cy, 3.5, cy - 10, cy + 10), 50, 1030))
                await self._mouse.move(page, nx, ny, speed_factor=0.22)

    async def read_page(self, page: Page, word_count: Optional[int] = None) -> None:
        """
        Simulate reading: scroll in chunks, pausing between each
        proportional to text density. Uses the shared mouse instance.
        """
        if word_count is None:
            try:
                text = await page.evaluate("document.body.innerText")
                word_count = len(text.split())
            except Exception:
                word_count = 300

        read_time = (word_count / self.profile.reading_wpm) * 60
        read_time *= _gauss_clamp(1.0, 0.2, 0.5, 1.8)

        scroll_h = await page.evaluate("document.body.scrollHeight")
        viewport_h = await page.evaluate("window.innerHeight")
        total_scroll = max(0, scroll_h - viewport_h)

        if total_scroll > 0:
            n_scrolls = max(3, int(total_scroll / 300))
            scroll_pause = read_time / n_scrolls
            per_scroll = total_scroll / n_scrolls
            for _ in range(n_scrolls):
                await self._mouse.scroll(page, int(per_scroll))
                await self.think(
                    int(max(500, scroll_pause * 800)),
                    int(max(1500, scroll_pause * 1200)),
                    page=page,
                )
        else:
            await self.think(int(read_time * 800), int(read_time * 1200), page=page)

    async def idle(self, page: Page, duration_s: float) -> None:
        """
        Simulate idle presence: occasional small mouse drifts,
        no significant actions.
        """
        end_time = time.time() + duration_s
        while time.time() < end_time:
            wait = _gauss_clamp(4.0, 2.0, 1.0, 12.0)
            await asyncio.sleep(min(wait, end_time - time.time()))
            if time.time() >= end_time:
                break
            cx, cy = self._mouse._pos
            nx = _gauss_clamp(cx, 30, 50, 1870)
            ny = _gauss_clamp(cy, 20, 50, 1030)
            await self._mouse.move(page, nx, ny, speed_factor=0.6)

    async def natural_navigate(
        self,
        page: Page,
        url: str,
        wait_for_load: bool = True,
    ) -> dict:
        """
        Navigate to URL, confirm the page actually settled there, simulate
        initial scan.  Returns:
          {
            "requested": <original url>,
            "url":       <final stable url>,
            "stable":    <True if final domain matches requested domain>,
          }
        """
        await self.think(200, 800, page=page)
        await page.goto(url, wait_until="domcontentloaded" if wait_for_load else "commit")
        if wait_for_load:
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # Heavy SPAs never reach networkidle — domcontentloaded is enough
        # Wait for URL to stop changing — catches JS redirects that fire
        # post-domcontentloaded (bot gates, SPA routers, tracker bounces).
        final_url = await _wait_stable_url(page, timeout_ms=3000, settle_ms=500)
        requested_domain = _url_domain(url)
        final_domain = _url_domain(final_url)
        stable = bool(
            requested_domain and final_domain and
            (requested_domain == final_domain or
             requested_domain.endswith("." + final_domain) or
             final_domain.endswith("." + requested_domain))
        )
        await self.think(500, 2000, page=page)
        for _ in range(random.randint(1, 3)):
            nx = random.uniform(200, 1700)
            ny = random.uniform(100, 900)
            await self._mouse.move(page, nx, ny, speed_factor=0.8)
            await self.think(200, 800, page=page)
        return {"requested": url, "url": final_url, "stable": stable}
