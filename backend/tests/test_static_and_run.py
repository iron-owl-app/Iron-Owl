from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import run
from app.main import create_app
from tests.conftest import BASE_URL, make_settings


@pytest.fixture
def spa_client(tmp_path, fake_plaid):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>FinTrack</title>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "secret.txt").write_text("outside dist")
    return TestClient(create_app(make_settings(tmp_path), fake_plaid), base_url=BASE_URL)


def test_serves_index_and_assets(spa_client):
    r = spa_client.get("/")
    assert r.status_code == 200 and "FinTrack" in r.text
    assert r.headers["x-frame-options"] == "DENY"
    r = spa_client.get("/assets/app.js")
    assert r.status_code == 200 and "console.log" in r.text


def test_spa_fallback_but_not_for_api(spa_client):
    r = spa_client.get("/transactions")
    assert r.status_code == 200 and "FinTrack" in r.text
    r = spa_client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_no_path_traversal(spa_client):
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
        r = spa_client.get(path)
        assert "outside dist" not in r.text


def test_no_frontend_dist_gives_404(client):
    assert client.get("/").status_code == 404


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_run_refuses_non_loopback(monkeypatch, tmp_path, host):
    monkeypatch.setattr(run, "get_settings", lambda: make_settings(tmp_path, host=host))
    monkeypatch.setattr(run.uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert "non-loopback" in str(exc.value)


def test_run_binds_loopback(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(run, "get_settings", lambda: make_settings(tmp_path, host="localhost", port=8123))
    monkeypatch.setattr(run.uvicorn, "run", lambda app, **k: seen.update(k))
    run.main()
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8123
