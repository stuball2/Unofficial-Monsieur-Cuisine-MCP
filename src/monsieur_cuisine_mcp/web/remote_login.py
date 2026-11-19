"""Remote login: a real (headed) Chrome on a virtual X display, streamed to a web page.

The Lidl sign-in sits behind Akamai Bot Manager, so the login has to happen in a
genuine browser driven by a human. This runs that browser on the server (Xvfb, no
monitor needed), streams its screen to the web interface with the Chrome DevTools
screencast, and replays the user's clicks and typing into it. Nothing here types
credentials or interacts with the page on its own.

When the Monsieur Cuisine session cookie appears, the browser state is saved
(owner-only permissions) and everything is shut down again.
"""
import asyncio
import json
import re
import logging
import os
import shutil
import time

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .. import auth
from ..config import (
    MC_AUTHENTICATION_TIMEOUT_SECONDS,
    MC_BROWSER_PROFILE_DIR,
    MC_SSO_LOGIN_URL,
    MC_WEB_BROWSER_LOCALE,
)
from . import session

logger = logging.getLogger(__name__)

BROWSER_UI_HEIGHT = 110  # Chrome's tab strip + toolbar inside the virtual screen

# DevTools key definitions for the non-text keys the page offers.
SPECIAL_KEYS = {
    "Enter": ("Enter", 13, "\r"),
    "Backspace": ("Backspace", 8, None),
    "Tab": ("Tab", 9, None),
    "Escape": ("Escape", 27, None),
    "Delete": ("Delete", 46, None),
    "ArrowLeft": ("ArrowLeft", 37, None),
    "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None),
    "ArrowDown": ("ArrowDown", 40, None),
}


def saved_credential(field: str) -> str:
    """Lidl email/password the user chose to keep in the service's env file
    (MC_LIDL_EMAIL / MC_LIDL_PASSWORD). Only ever typed into the remote browser,
    never sent to the web page."""
    return {"email": os.environ.get("MC_LIDL_EMAIL", ""),
            "password": os.environ.get("MC_LIDL_PASSWORD", "")}.get(field, "")


# Lidl's sign-in asks for the email first, then (after "Next") the password.
AUTOFILL_SELECTORS = {
    "email": "#input-email, input[type=email], input[autocomplete=username]",
    "password": "input[type=password]",
}
# Focus the first visible, editable, still-empty match; report whether we did.
_FOCUS_EMPTY_FIELD_JS = """(sel) => {
  const el = [...document.querySelectorAll(sel)].find(e => e.offsetParent !== null && !e.disabled && !e.readOnly);
  if (!el || el.value) return false;
  el.focus();
  return true;
}"""


NEXT_BUTTON = r"^\s*(next|continue)\s*$"
SIGN_IN_BUTTON = r"^\s*(sign in|log in|login)\s*$"  # anchored: must not match "Login via phone"


def has_saved_credentials() -> bool:
    return bool(saved_credential("email") and saved_credential("password"))


def clamp_size(width: int, height: int) -> tuple[int, int]:
    """Viewport size for the remote browser, based on the visitor's screen.
    Chrome won't make windows narrower than ~500px, and huge ones waste bandwidth."""
    return max(500, min(int(width or 1024), 1280)), max(600, min(int(height or 800), 1000))


def to_page_coords(nx: float, ny: float, meta: dict) -> tuple[float, float]:
    """Map normalised (0..1) coordinates on the streamed image to page CSS pixels."""
    nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
    return nx * meta.get("deviceWidth", 0), ny * meta.get("deviceHeight", 0)


def key_events(key: str) -> list[dict]:
    if key not in SPECIAL_KEYS:
        raise ValueError(f"Unsupported key {key!r}")
    name, code, text = SPECIAL_KEYS[key]
    down = {"type": "keyDown", "key": name, "code": name, "windowsVirtualKeyCode": code,
            "nativeVirtualKeyCode": code}
    if text:
        down["text"] = text
    up = {"type": "keyUp", "key": name, "code": name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
    return [down, up]


async def start_xvfb(width: int, height: int) -> tuple[str, asyncio.subprocess.Process]:
    """Start Xvfb on a free display number; return (":N", process)."""
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb is not installed (sudo apt install xvfb).")
    read_fd, write_fd = os.pipe()
    proc = await asyncio.create_subprocess_exec(
        "Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp",
        pass_fds=(write_fd,), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    os.close(write_fd)
    loop = asyncio.get_running_loop()
    try:
        raw = await asyncio.wait_for(loop.run_in_executor(None, os.read, read_fd, 32), timeout=10)
    finally:
        os.close(read_fd)
    if not raw.strip():
        proc.kill()
        raise RuntimeError("Xvfb failed to start.")
    return f":{raw.decode().strip()}", proc


class RemoteLogin:
    """One login session at a time, shared by every connected viewer."""

    def __init__(self) -> None:
        self.state = "idle"        # idle | starting | auto | waiting | success | failed | cancelled
        self.auto = False
        self.message = ""
        self.url = ""
        self.started_at = 0.0
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._viewers: set = set()   # websockets
        self._cdp = None
        self._meta: dict = {}
        self._last_frame: str | None = None
        self._page = None
        self._filled: set[str] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def info(self) -> dict:
        return {"type": "status", "state": self.state, "message": self.message, "url": self.url}

    # ------------------------------------------------------------ lifecycle
    def start(self, width: int, height: int, auto: bool = False) -> None:
        """auto=True signs in with the saved email/password and only hands over
        to the human if something unexpected appears (OTP, error, challenge)."""
        if self.running:
            return
        self.auto = auto and has_saved_credentials()
        self._cancel = asyncio.Event()
        self.state, self.message, self.url = "starting", "Starting browser…", ""
        self._last_frame, self._meta = None, {}
        self._filled = set()
        self.started_at = time.time()
        self._task = asyncio.create_task(self._run(*clamp_size(width, height)))

    def cancel(self) -> None:
        if self.running:
            self._cancel.set()

    async def _set(self, state: str, message: str) -> None:
        self.state, self.message = state, message
        await self._broadcast(self.info())

    async def _run(self, width: int, height: int) -> None:
        xvfb = None
        try:
            display, xvfb = await start_xvfb(width, height + BROWSER_UI_HEIGHT)
            async with async_playwright() as pw:
                context = await self._launch(pw, display, width, height)
                try:
                    await self._drive(context)
                finally:
                    try:
                        await context.close()
                    except PlaywrightError:
                        pass
        except asyncio.CancelledError:
            await self._set("cancelled", "Login cancelled.")
            raise
        except Exception as e:  # noqa: BLE001 - surface any failure on the page
            logger.exception("Remote login failed")
            await self._set("failed", f"{type(e).__name__}: {e}")
            session.record("login", False, str(e))
        finally:
            self._cdp, self._page = None, None
            if xvfb and xvfb.returncode is None:
                xvfb.terminate()
                try:
                    await asyncio.wait_for(xvfb.wait(), 5)
                except asyncio.TimeoutError:
                    xvfb.kill()

    async def _launch(self, pw, display: str, width: int, height: int):
        MC_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        options = {
            "user_data_dir": str(MC_BROWSER_PROFILE_DIR),
            "headless": False,
            "args": [*auth._ANTI_AUTOMATION_ARGS, "--window-position=0,0",
                     f"--window-size={width},{height + BROWSER_UI_HEIGHT}",
                     "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                     "--disable-features=PasswordManagerOnboarding"],
            "bypass_csp": True,
            "locale": MC_WEB_BROWSER_LOCALE,
            "no_viewport": True,
            "env": {**os.environ, "DISPLAY": display},
        }
        try:
            return await pw.chromium.launch_persistent_context(channel="chrome", **options)
        except PlaywrightError as e:
            logger.warning("Google Chrome unavailable (%s); falling back to bundled Chromium", e)
            return await pw.chromium.launch_persistent_context(**options)

    async def _drive(self, context) -> None:
        context.on("page", lambda p: asyncio.create_task(self._attach(p)))
        page = context.pages[0] if context.pages else await context.new_page()
        await self._attach(page)
        await page.goto(MC_SSO_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        if self.auto:
            await self._set("auto", "Signing in automatically…")
            if not await self._auto_login(context, page):
                await self._set("waiting", "Automatic sign-in needs your help - please take over in the window below.")
        else:
            await self._set("waiting", "Sign in to your Lidl account in the window below.")

        deadline = time.monotonic() + MC_AUTHENTICATION_TIMEOUT_SECONDS
        while True:
            if self._cancel.is_set():
                await self._set("cancelled", "Login cancelled.")
                return
            if auth._has_session_cookie(await context.cookies()):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"No login within {MC_AUTHENTICATION_TIMEOUT_SECONDS // 60} minutes.")
            await self._autofill()
            if self._page and self._page.url != self.url:
                self.url = self._page.url
                await self._broadcast(self.info())
            await asyncio.sleep(1.5)

        await asyncio.sleep(3)  # let the post-login redirects finish setting cookies
        session.save_storage_state(await context.storage_state())
        session.record("login", True, "Logged in automatically." if self.auto and self.state == "auto"
                       else "Logged in via web interface.")
        await self._set("success", "Logged in - session saved.")
        await session.check_session()

    async def _session_appeared(self, context, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if auth._has_session_cookie(await context.cookies()):
                return True
            await asyncio.sleep(1)
        return False

    async def _press(self, key: str) -> None:
        for ev in key_events(key):
            await self._cdp.send("Input.dispatchKeyEvent", ev)

    @staticmethod
    async def _click_button(page, pattern: str) -> bool:
        button = page.get_by_role("button", name=re.compile(pattern, re.I)).first
        try:
            await button.click(timeout=5000)
            return True
        except PlaywrightError:
            return False

    async def _auto_login(self, context, page) -> bool:
        """Email → Next → password → Sign in, in the real browser. Returns False
        (hand over to the human) at the first step that doesn't go as expected."""
        email = page.locator(AUTOFILL_SELECTORS["email"]).first
        password = page.locator(AUTOFILL_SELECTORS["password"]).first
        try:
            await email.wait_for(state="visible", timeout=20000)
            await self._autofill()
            await self._press("Enter")
            try:
                await password.wait_for(state="visible", timeout=6000)
            except PlaywrightError:
                await self._click_button(page, NEXT_BUTTON)
                await password.wait_for(state="visible", timeout=15000)
            await self._autofill()
            await self._press("Enter")
            if await self._session_appeared(context, 12):
                return True
            if not await self._click_button(page, SIGN_IN_BUTTON):
                return False
            return await self._session_appeared(context, 30)
        except (PlaywrightError, TimeoutError) as e:
            logger.info("Automatic sign-in stopped: %s", e)
            return False

    async def _autofill(self) -> None:
        """Type the saved email/password into Lidl's fields when they appear empty.

        Each field is filled at most once per login attempt, so a field the user
        clears (e.g. to use a different account) stays clear. Never presses
        Next / Sign in - the user does that.
        """
        page, cdp = self._page, self._cdp
        if not page or not cdp:
            return
        for field, selector in AUTOFILL_SELECTORS.items():
            value = saved_credential(field)
            if not value or field in self._filled:
                continue
            try:
                if await page.evaluate(_FOCUS_EMPTY_FIELD_JS, selector):
                    await cdp.send("Input.insertText", {"text": value})
                    self._filled.add(field)
                    await self._broadcast({**self.info(), "message": f"Filled in your saved {field}. "
                                           + ("Press Next." if field == "email" else "Press Sign in.")})
            except PlaywrightError:
                pass  # page mid-navigation; try again next poll

    # ------------------------------------------------------------ screen streaming
    async def _attach(self, page) -> None:
        """Stream this page (the newest tab/popup wins)."""
        if self._cdp:
            try:
                await self._cdp.send("Page.stopScreencast")
            except PlaywrightError:
                pass
        self._page = page
        cdp = await page.context.new_cdp_session(page)
        self._cdp = cdp

        def on_frame(params: dict) -> None:
            asyncio.create_task(self._ack(cdp, params["sessionId"]))
            if cdp is not self._cdp:
                return
            self._meta = params.get("metadata", {})
            self._last_frame = params["data"]
            asyncio.create_task(self._broadcast({"type": "frame", "data": params["data"]}))

        cdp.on("Page.screencastFrame", on_frame)
        page.on("close", lambda _: asyncio.create_task(self._on_close(page)))
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70,
                                                 "maxWidth": 1600, "maxHeight": 1600, "everyNthFrame": 1})

    async def _on_close(self, page) -> None:
        if page is self._page and page.context.pages:
            await self._attach(page.context.pages[-1])

    @staticmethod
    async def _ack(cdp, session_id: int) -> None:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except PlaywrightError:
            pass

    # ------------------------------------------------------------ viewers & input
    async def add_viewer(self, ws) -> None:
        self._viewers.add(ws)
        await ws.send_text(json.dumps(self.info()))
        if self._last_frame:
            await ws.send_text(json.dumps({"type": "frame", "data": self._last_frame}))

    def remove_viewer(self, ws) -> None:
        self._viewers.discard(ws)

    async def _broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        for ws in list(self._viewers):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001 - viewer went away
                self._viewers.discard(ws)

    async def handle_input(self, msg: dict) -> None:
        cdp = self._cdp
        if not cdp or self.state not in ("waiting", "auto"):
            return
        kind = msg.get("type")
        try:
            if kind == "click":
                x, y = to_page_coords(float(msg["x"]), float(msg["y"]), self._meta)
                base = {"x": x, "y": y, "button": "left", "clickCount": 1}
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})
            elif kind == "wheel":
                x, y = to_page_coords(float(msg["x"]), float(msg["y"]), self._meta)
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                             "deltaX": 0, "deltaY": float(msg["dy"])})
            elif kind == "text":
                text = str(msg.get("text", ""))[:500]
                if text:
                    await cdp.send("Input.insertText", {"text": text})
            elif kind == "fill":
                value = saved_credential(str(msg.get("field")))
                if value:
                    await cdp.send("Input.insertText", {"text": value})
            elif kind == "key":
                for ev in key_events(str(msg.get("key"))):
                    await cdp.send("Input.dispatchKeyEvent", ev)
            elif kind == "back" and self._page:
                await self._page.go_back()
            elif kind == "reload" and self._page:
                await self._page.reload()
        except (PlaywrightError, ValueError, KeyError) as e:
            logger.debug("Input %s ignored: %s", kind, e)
