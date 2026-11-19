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
    assert "Not logged in" in r.text and 'href="/login"' in r.text


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
