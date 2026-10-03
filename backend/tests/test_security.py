from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security import CSP, SESSION_HEADER
from tests.conftest import BASE_URL, PASSWORD, make_settings


def test_csrf_header_required(client):
    r = client.post("/api/auth/setup", json={"password": PASSWORD}, headers={"X-FinTrack": ""})
    assert r.status_code == 403
    raw = TestClient(client.app, base_url=BASE_URL)  # no default CSRF header
    r = raw.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 403 and r.json() == {"detail": "forbidden"}
    assert raw.post("/api/auth/lock").status_code == 403
    assert raw.get("/api/auth/status").status_code == 200  # GET is exempt


def test_csrf_header_required_on_data_routes(unlocked):
    raw = TestClient(
        unlocked.app,
        base_url=BASE_URL,
        cookies=dict(unlocked.cookies),
        headers={SESSION_HEADER: unlocked.headers[SESSION_HEADER]},
    )
    body = {"name": "x", "category": "bank", "current_balance": 1}
    assert raw.post("/api/accounts", json=body).status_code == 403
    assert raw.delete("/api/accounts/1").status_code == 403
    assert raw.patch("/api/accounts/1", json={"hidden": True}).status_code == 403
    assert raw.get("/api/accounts").status_code == 200


def test_origin_must_be_allowlisted(client):
    r = client.post("/api/auth/setup", json={"password": PASSWORD},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/auth/setup", json={"password": PASSWORD},
                    headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 403  # dev origin only allowed with DEV=1
    r = client.post("/api/auth/setup", json={"password": PASSWORD},
                    headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1:9999",
                                  "localhost", "192.168.1.10:8000", ""])
def test_bad_host_rejected(client, host):
    r = client.get("/api/auth/status", headers={"Host": host})
    assert r.status_code == 400
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "LOCALHOST:8000"])
def test_good_host_accepted(client, host):
    assert client.get("/api/auth/status", headers={"Host": host}).status_code == 200


def test_dev_mode_allows_vite(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path, dev=True), fake_plaid)
    c = TestClient(app, base_url="http://localhost:5173", headers={"X-FinTrack": "1"})
    assert c.get("/api/auth/status").status_code == 200
    r = c.post("/api/auth/setup", json={"password": PASSWORD},
               headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    app.state.fintrack.vault.lock()


def test_security_headers(client, tmp_path):
    r = client.get("/api/auth/status")
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    # Error responses too.
    r = client.get("/api/accounts")
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == CSP


def test_docs_disabled_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_enabled_in_debug(tmp_path, fake_plaid):
    c = TestClient(create_app(make_settings(tmp_path, debug=True), fake_plaid), base_url=BASE_URL)
    assert c.get("/openapi.json").status_code == 200


def test_unknown_api_route_is_json_404(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_leaked_cookie_without_session_token_is_useless(unlocked):
    """Another server on a different 127.0.0.1 port receives our cookie (cookies ignore ports)."""
    thief = TestClient(unlocked.app, base_url=BASE_URL, cookies=dict(unlocked.cookies), headers={"X-FinTrack": "1"})
    assert thief.get("/api/accounts").status_code == 401
    assert thief.get("/api/auth/status").json()["unlocked"] is False
    thief.headers[SESSION_HEADER] = "guessed-token"
    assert thief.get("/api/accounts").status_code == 401
    # The legitimate tab (cookie + token) is unaffected.
    assert unlocked.get("/api/accounts").status_code == 200


def test_token_without_cookie_is_rejected(unlocked):
    c = TestClient(unlocked.app, base_url=BASE_URL, headers={SESSION_HEADER: unlocked.headers[SESSION_HEADER]})
    assert c.get("/api/accounts").status_code == 401


def test_session_token_rotates_and_dies_on_lock(unlocked):
    first = unlocked.headers[SESSION_HEADER]
    unlocked.post("/api/auth/lock")
    stale = TestClient(unlocked.app, base_url=BASE_URL, headers={SESSION_HEADER: first})
    assert stale.get("/api/accounts").status_code == 401
    unlocked.post("/api/auth/unlock", json={"password": PASSWORD})
    assert unlocked.headers[SESSION_HEADER] != first
    assert unlocked.get("/api/accounts").status_code == 200
