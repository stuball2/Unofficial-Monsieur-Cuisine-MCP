"""Saved-session housekeeping for the web interface: status, health check, renewal.

The saved session is Playwright's storage_state JSON (all cookies from the login
browser). Only the monsieur-cuisine.com cookies are used for API calls; the
accounts.lidl.com cookies are what make silent renewal possible.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from http.cookiejar import Cookie
from pathlib import Path

import httpx

from .. import auth, recipes
from ..config import (
    MC_AUTHENTICATION_SSO_DOMAIN,
    MC_BROWSER_USER_AGENT,
    MC_DOMAIN,
    MC_SESSION_COOKIE_PREFIXES,
    MC_SSO_LOGIN_FORM_PATH,
    MC_SSO_LOGIN_URL,
    MC_WEB_STATUS_PATH,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ files
def storage_path() -> Path:
    # Read through the auth module so tests (and auth) share one source of truth.
    return auth.STORAGE_STATE_PATH


def write_private_json(path: Path, data: dict) -> None:
    """Atomically write JSON readable only by the owner (it holds session cookies)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_storage_state() -> dict | None:
    path = storage_path()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_storage_state(state: dict) -> None:
    write_private_json(storage_path(), state)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_status() -> dict:
    try:
        return json.loads(Path(MC_WEB_STATUS_PATH).read_text())
    except (OSError, ValueError):
        return {}


def record(kind: str, ok: bool, message: str) -> dict:
    """Remember the outcome of a check / renew / login for the status page."""
    status = load_status()
    status[kind] = {"at": now_iso(), "ok": ok, "message": message[:2000]}
    write_private_json(Path(MC_WEB_STATUS_PATH), status)
    return status[kind]


# ------------------------------------------------------------------ status
def _is_session_cookie(c: dict) -> bool:
    return c["domain"].lstrip(".").endswith(MC_DOMAIN) and c["name"].startswith(MC_SESSION_COOKIE_PREFIXES)


def _expiry(c: dict) -> str | None:
    exp = c.get("expires", -1)
    if not exp or exp < 0:
        return None  # browser-session cookie, no fixed expiry
    return datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="minutes")


def session_summary(state: dict | None = None, now: float | None = None) -> dict:
    """What the status page shows about the saved session. Never includes cookie values."""
    state = state if state is not None else load_storage_state()
    now = now if now is not None else time.time()
    if not state:
        return {"saved": False}
    cookies = state.get("cookies", [])
    live = [c for c in cookies if not (c.get("expires", -1) > 0 and c["expires"] < now)]
    mc = [c for c in live if _is_session_cookie(c)]
    sso = [c for c in live if c["domain"].lstrip(".").endswith(MC_AUTHENTICATION_SSO_DOMAIN)]
    saved_at = datetime.fromtimestamp(storage_path().stat().st_mtime, timezone.utc).isoformat(timespec="seconds") \
        if storage_path().exists() else None
    return {
        "saved": True,
        "saved_at": saved_at,
        "has_mc_session": bool(mc),
        "mc_cookies": [{"name": c["name"], "expires": _expiry(c)} for c in mc],
        "sso_cookie_count": len(sso),
        "sso_latest_expiry": max((_expiry(c) for c in sso if _expiry(c)), default=None),
    }


# ------------------------------------------------------------------ check
async def check_session() -> dict:
    """Make one cheap authenticated call (first favourite) to prove the session works."""
    try:
        await recipes.list_favorite_recipes(page=1, page_size=1)
    except auth.NotLoggedInError:
        return record("check", False, "No saved session - log in.")
    except recipes.MCRequestError as e:
        return record("check", False, str(e))
    except httpx.HTTPError as e:
        return record("check", False, f"Network error: {e!r}")
    return record("check", True, "Authenticated call succeeded.")


# ------------------------------------------------------------------ renew
def _to_httpx_cookies(cookies: list[dict], now: float) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in cookies:
        if c.get("expires", -1) > 0 and c["expires"] < now:
            continue
        jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return jar


def _same_domain(a: str, b: str) -> bool:
    return a.lstrip(".") == b.lstrip(".")


def merge_cookies(state: dict, jar: httpx.Cookies) -> int:
    """Copy cookies from an httpx jar back into a Playwright storage_state. Returns #changed."""
    cookies: list[dict] = state.setdefault("cookies", [])
    changed = 0
    for ck in jar.jar:  # http.cookiejar.Cookie
        ck: Cookie
        existing = next((c for c in cookies if c["name"] == ck.name and _same_domain(c["domain"], ck.domain)
                         and c.get("path", "/") == ck.path), None)
        expires = float(ck.expires) if ck.expires else -1
        if existing:
            if existing["value"] != ck.value or existing.get("expires", -1) != expires:
                existing["value"], existing["expires"] = ck.value, expires
                changed += 1
        else:
            cookies.append({"name": ck.name, "value": ck.value, "domain": ck.domain, "path": ck.path,
                            "expires": expires, "httpOnly": bool(ck.has_nonstandard_attr("HttpOnly")),
                            "secure": bool(ck.secure), "sameSite": "Lax"})
            changed += 1
    return changed


async def renew_session() -> dict:
    """Re-run the SSO redirect chain with the saved Lidl cookies (no password).

    If accounts.lidl.com still recognises the browser it redirects straight back
    with a fresh Monsieur Cuisine session; if it shows the login form instead,
    a human has to log in again via the web interface.
    """
    state = load_storage_state()
    if not state:
        return record("renew", False, "No saved session - log in.")
    now = time.time()
    headers = {"User-Agent": MC_BROWSER_USER_AGENT, "Accept-Language": "en-GB,en;q=0.9",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    try:
        async with httpx.AsyncClient(cookies=_to_httpx_cookies(state["cookies"], now), headers=headers,
                                     follow_redirects=True, timeout=30) as client:
            resp = await client.get(MC_SSO_LOGIN_URL)
            jar = client.cookies
    except httpx.HTTPError as e:
        return record("renew", False, f"Network error during renewal: {e!r}")

    final = resp.url
    hops = " → ".join(h.url.host for h in resp.history) + f" → {final.host}"
    if final.host.endswith(MC_AUTHENTICATION_SSO_DOMAIN) and final.path.startswith(MC_SSO_LOGIN_FORM_PATH):
        return record("renew", False, "Lidl sign-in has expired - please log in again.")
    if resp.status_code >= 400:
        return record("renew", False, f"Renewal ended with HTTP {resp.status_code} at {final.host}{final.path}")

    merge_cookies(state, jar)
    if not any(_is_session_cookie(c) for c in state["cookies"]):
        return record("renew", False, f"Redirects finished ({hops}) but no Monsieur Cuisine session cookie was set.")
    save_storage_state(state)
    return record("renew", True, f"Session renewed ({hops}).")


async def check_and_heal() -> None:
    """Periodic job: check; if broken and a session exists, renew and re-check."""
    result = await check_session()
    if result["ok"] or not load_storage_state():
        return
    renewal = await renew_session()
    if renewal["ok"]:
        await check_session()
