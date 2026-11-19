"""Shared test setup. Every test runs fully offline:

* `respx` intercepts all httpx traffic; any request without an explicit mock fails.
* Raw socket connections to anything but localhost are refused.
* HOME points at a temp dir so a real saved login session is never read.
"""
import json
import socket
from pathlib import Path

import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_COOKIES = {"wordpress_logged_in_test": "fake-session"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost") and not isinstance(address, str):
            raise RuntimeError(f"Live network access blocked in tests: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from monsieur_cuisine_mcp import auth
    monkeypatch.setattr(auth, "STORAGE_STATE_PATH", tmp_path / "storage_state.json", raising=False)
    from monsieur_cuisine_mcp.web import session
    monkeypatch.setattr(session, "MC_WEB_STATUS_PATH", tmp_path / "web_status.json")


@pytest.fixture(autouse=True)
def mock_http():
    """All httpx requests go through this router. Register routes per test, e.g.
    `mock_http.get(url).respond(json=...)`. Unmatched requests raise."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text())
    return _load


@pytest.fixture
def logged_in(monkeypatch):
    """Pretend a login session exists (fake cookies, nothing read from disk)."""
    from monsieur_cuisine_mcp import auth
    monkeypatch.setattr(auth, "load_session_cookies", lambda: dict(FAKE_COOKIES))
    return FAKE_COOKIES


@pytest.fixture
def logged_out(monkeypatch):
    from monsieur_cuisine_mcp import auth

    def _raise():
        raise auth.NotLoggedInError()
    monkeypatch.setattr(auth, "load_session_cookies", _raise)
