import base64

import pytest
from starlette.testclient import TestClient

from monsieur_cuisine_mcp.web import app as web
from monsieur_cuisine_mcp.web import remote_login as rl

SECRET = "correct-horse-battery"


@pytest.fixture
def client():
    with TestClient(web.create_app(SECRET, with_mcp=False, background=False)) as c:
        yield c


def basic(pw):
    return {"Authorization": "Basic " + base64.b64encode(f"stu:{pw}".encode()).decode()}


def test_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/", headers=basic("wrong")).status_code == 401


def test_basic_and_bearer_accepted(client):
    assert client.get("/healthz", headers=basic(SECRET)).status_code == 200
    assert client.get("/healthz", headers={"Authorization": f"Bearer {SECRET}"}).status_code == 200


def test_status_page_when_logged_out(client):
    r = client.get("/", headers=basic(SECRET))
    assert "Not logged in" in r.text and 'action="/login/go"' in r.text


def test_login_page_embeds_signed_ws_token(client):
    r = client.get("/login", headers=basic(SECRET))
    assert r.status_code == 200 and "const TOKEN=" in r.text


def test_websocket_rejects_missing_token(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/login/ws") as ws:
            ws.receive_text()


def test_websocket_with_token_gets_status(client):
    token = web.sign_ws_token(SECRET)
    with client.websocket_connect(f"/login/ws?token={token}") as ws:
        assert '"state": "idle"' in ws.receive_text()


def test_ws_token_expiry_and_tamper():
    tok = web.sign_ws_token(SECRET, now=1000)
    assert web.verify_ws_token(SECRET, tok, now=1000)
    assert not web.verify_ws_token(SECRET, tok, now=1000 + web.WS_TOKEN_TTL + 1)
    assert not web.verify_ws_token("other-secret", tok, now=1000)
    assert not web.verify_ws_token(SECRET, "garbage", now=1000)


def test_clamp_and_coordinate_mapping():
    assert rl.clamp_size(390, 700) == (500, 700)
    assert rl.clamp_size(4000, 4000) == (1280, 1000)
    assert rl.to_page_coords(0.5, 2.0, {"deviceWidth": 800, "deviceHeight": 600}) == (400, 600)


def test_key_events():
    down, up = rl.key_events("Enter")
    assert down["type"] == "keyDown" and down["text"] == "\r" and up["type"] == "keyUp"
    with pytest.raises(ValueError):
        rl.key_events("F13")


def test_no_auth_mode_serves_without_credentials():
    with TestClient(web.create_app(None, with_mcp=False, background=False)) as c:
        assert c.get("/").status_code == 200
        with c.websocket_connect("/login/ws") as ws:
            assert '"state": "idle"' in ws.receive_text()


def test_fill_buttons_only_when_credentials_saved(client, monkeypatch):
    monkeypatch.delenv("MC_LIDL_EMAIL", raising=False)
    monkeypatch.delenv("MC_LIDL_PASSWORD", raising=False)
    assert "data-fill=\"" not in client.get("/login", headers=basic(SECRET)).text
    monkeypatch.setenv("MC_LIDL_EMAIL", "me@example.test")
    monkeypatch.setenv("MC_LIDL_PASSWORD", "hunter2-secret")
    html = client.get("/login", headers=basic(SECRET)).text
    assert 'data-fill="email"' in html and 'data-fill="password"' in html
    # The values themselves never reach the page.
    assert "me@example.test" not in html and "hunter2-secret" not in html


async def test_fill_inserts_saved_value_into_remote_browser(monkeypatch):
    monkeypatch.setenv("MC_LIDL_PASSWORD", "hunter2-secret")
    sent = []

    class FakeCDP:
        async def send(self, method, params=None):
            sent.append((method, params))

    login = rl.RemoteLogin()
    login._cdp, login.state = FakeCDP(), "waiting"
    await login.handle_input({"type": "fill", "field": "password"})
    await login.handle_input({"type": "fill", "field": "bogus"})
    assert sent == [("Input.insertText", {"text": "hunter2-secret"})]


class FakeLogin:
    running = False

    def __init__(self):
        self.started = []

    def start(self, w, h, auto=False):
        self.started.append(auto)


def test_auto_login_needs_creds_and_failed_check(monkeypatch):
    from monsieur_cuisine_mcp.web import session
    login = FakeLogin()
    monkeypatch.delenv("MC_LIDL_EMAIL", raising=False)
    monkeypatch.delenv("MC_LIDL_PASSWORD", raising=False)
    session.record("check", False, "expired")
    assert web.maybe_auto_login(login) is False          # no saved credentials
    monkeypatch.setenv("MC_LIDL_EMAIL", "me@example.test")
    monkeypatch.setenv("MC_LIDL_PASSWORD", "pw")
    session.record("check", True, "fine")
    assert web.maybe_auto_login(login) is False          # session is healthy
    session.record("check", False, "expired")
    assert web.maybe_auto_login(login) is True and login.started == [True]


def test_auto_login_rate_limited_and_can_be_disabled(monkeypatch):
    import time as _t
    from monsieur_cuisine_mcp.web import session
    monkeypatch.setenv("MC_LIDL_EMAIL", "me@example.test")
    monkeypatch.setenv("MC_LIDL_PASSWORD", "pw")
    session.record("check", False, "expired")
    login = FakeLogin()
    assert web.maybe_auto_login(login) is True
    assert web.maybe_auto_login(login) is False                                   # within 12h
    assert web.maybe_auto_login(login, now=_t.time() + 13 * 3600) is True         # after 12h
    monkeypatch.setenv("MC_WEB_AUTO_LOGIN", "0")
    assert web.maybe_auto_login(login, now=_t.time() + 30 * 3600) is False


def test_status_page_log_in_button_posts_to_go(client):
    assert 'action="/login/go"' in client.get("/", headers=basic(SECRET)).text
