import json
import stat
import time

import httpx

from monsieur_cuisine_mcp import auth
from monsieur_cuisine_mcp.config import MC_PROXY_API_URL, MC_SSO_LOGIN_URL
from monsieur_cuisine_mcp.web import session

FUTURE = time.time() + 86400 * 10
PAST = time.time() - 60


def make_state(**extra):
    cookies = [
        {"name": "wordpress_logged_in_abc", "value": "SECRET-MC", "domain": "www.monsieur-cuisine.com",
         "path": "/", "expires": FUTURE},
        {"name": "idsrv.session", "value": "SECRET-SSO", "domain": "accounts.lidl.com", "path": "/",
         "expires": FUTURE},
        {"name": "old", "value": "x", "domain": "accounts.lidl.com", "path": "/", "expires": PAST},
    ]
    return {"cookies": cookies, "origins": [], **extra}


def test_save_is_owner_only_and_round_trips():
    session.save_storage_state(make_state())
    mode = stat.S_IMODE(auth.STORAGE_STATE_PATH.stat().st_mode)
    assert mode == 0o600
    assert session.load_storage_state()["cookies"][0]["value"] == "SECRET-MC"


def test_summary_never_contains_cookie_values_and_skips_expired():
    session.save_storage_state(make_state())
    s = session.session_summary()
    assert s["saved"] and s["has_mc_session"] and s["sso_cookie_count"] == 1
    assert "SECRET" not in json.dumps(s)


def test_summary_without_session():
    assert session.session_summary() == {"saved": False}


def test_merge_cookies_updates_and_adds():
    state = make_state()
    jar = httpx.Cookies()
    jar.set("wordpress_logged_in_abc", "NEW", domain="www.monsieur-cuisine.com", path="/")
    jar.set("fresh", "1", domain=".monsieur-cuisine.com", path="/")
    assert session.merge_cookies(state, jar) == 2
    by = {c["name"]: c for c in state["cookies"]}
    assert by["wordpress_logged_in_abc"]["value"] == "NEW" and by["fresh"]["value"] == "1"


async def test_renew_follows_sso_chain_and_saves_new_cookie(mock_http):
    session.save_storage_state(make_state())
    authorize = "https://accounts.lidl.com/connect/authorize?client_id=monsieurcuisinewebcompanionclient"
    callback = "https://www.monsieur-cuisine.com/sso/post/login/redirect/?code=abc&state=xyz"
    sso = mock_http.get(MC_SSO_LOGIN_URL).respond(302, headers={"Location": authorize})
    mock_http.get(authorize).respond(302, headers={"Location": callback})
    mock_http.get(callback).respond(302, headers={
        "Location": "https://www.monsieur-cuisine.com/en",
        "Set-Cookie": "wordpress_logged_in_abc=RENEWED; Path=/; Secure; HttpOnly"})
    mock_http.get("https://www.monsieur-cuisine.com/en").respond(200, text="<html>home</html>")

    result = await session.renew_session()

    assert result["ok"], result
    # The saved Lidl cookie was presented to accounts.lidl.com; expired ones were not.
    lidl_req = mock_http.calls[1].request
    assert "idsrv.session=SECRET-SSO" in lidl_req.headers["cookie"] and "old=" not in lidl_req.headers["cookie"]
    assert sso.called
    saved = {c["name"]: c["value"] for c in session.load_storage_state()["cookies"]}
    assert saved["wordpress_logged_in_abc"] == "RENEWED"
    assert session.load_status()["renew"]["ok"] is True


async def test_renew_reports_expired_lidl_login(mock_http):
    session.save_storage_state(make_state())
    login_form = "https://accounts.lidl.com/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback"
    mock_http.get(MC_SSO_LOGIN_URL).respond(302, headers={"Location": login_form})
    mock_http.get(login_form).respond(200, text="<form id='login-form'>")
    result = await session.renew_session()
    assert not result["ok"] and "log in again" in result["message"]
    # The stored session is left untouched.
    assert session.load_storage_state()["cookies"][0]["value"] == "SECRET-MC"


async def test_renew_without_session():
    assert (await session.renew_session())["ok"] is False


async def test_check_session_ok_and_failure(mock_http, monkeypatch):
    session.save_storage_state(make_state())
    route = mock_http.post(MC_PROXY_API_URL)
    route.respond(json={"data": {"recipes": []}})
    assert (await session.check_session())["ok"] is True
    route.respond(403, text="<html>blocked</html>")
    result = await session.check_session()
    assert result["ok"] is False and "expired" in result["message"]


async def test_check_and_heal_renews_when_check_fails(monkeypatch):
    session.save_storage_state(make_state())
    calls = []

    async def fake_check():
        calls.append("check")
        return {"ok": len(calls) > 1}

    async def fake_renew():
        calls.append("renew")
        return {"ok": True}

    monkeypatch.setattr(session, "check_session", fake_check)
    monkeypatch.setattr(session, "renew_session", fake_renew)
    await session.check_and_heal()
    assert calls == ["check", "renew", "check"]
