"""Who to call for help (Iron Owl 2.0.0): data/support_contact.json, PUT /api/settings/support-contact."""
from __future__ import annotations

import json

import pytest

from app.config import SUPPORT_CONTACT_MAX
from app.main import create_app
from app.services import support_contact as sc
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient, make_settings

ROUTE = "/api/settings/support-contact"


def status(c) -> dict:
    r = c.get("/api/auth/status")
    assert r.status_code == 200, r.text
    return r.json()


def put(c, value, **extra):
    return c.put(ROUTE, json={"contact": value, **extra})


@pytest.fixture
def unlocked_app(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path, support_contact="From settings"), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        yield app, c


def test_save_shows_at_once_and_while_locked(unlocked_app, tmp_path):
    app, c = unlocked_app
    assert status(c)["support_contact"] == "From settings"
    r = put(c, "  Sam 555-0100  ")
    assert r.status_code == 200 and r.json() == {"support_contact": "Sam 555-0100"}
    assert status(c)["support_contact"] == "Sam 555-0100"
    saved = json.loads((tmp_path / "data" / sc.FILE_NAME).read_text(encoding="utf-8"))
    assert saved == {"support_contact": "Sam 555-0100"}
    c.post("/api/auth/lock")
    assert status(c)["support_contact"] == "Sam 555-0100"  # readable while locked


def test_blank_means_no_one_named_and_beats_the_settings_file(unlocked_app, tmp_path, fake_plaid):
    app, c = unlocked_app
    r = put(c, "   ")
    assert r.status_code == 200 and r.json() == {"support_contact": None}
    assert status(c)["support_contact"] is None
    # a restart keeps the saved choice over SUPPORT_CONTACT from the settings file
    again = create_app(make_settings(tmp_path, support_contact="From settings"), fake_plaid)
    with SessionClient(again, base_url=BASE_URL, headers=CSRF) as c2:
        assert status(c2)["support_contact"] is None


def test_saved_name_wins_after_restart(tmp_path, fake_plaid):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / sc.FILE_NAME).write_text('{"support_contact": "Sam"}', encoding="utf-8")
    app = create_app(make_settings(tmp_path, support_contact="Other"), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert status(c)["support_contact"] == "Sam"


def test_installer_file_with_bom_and_unicode(tmp_path, fake_plaid):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / sc.FILE_NAME).write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"support_contact": "Zoë – 555"}).encode("utf-8"))
    app = create_app(make_settings(tmp_path), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert status(c)["support_contact"] == "Zoë – 555"


@pytest.mark.parametrize("content", [b"", b"not json", b"[]", b'{"support_contact": 5}', b'{"other": "x"}',
                                     b'{"support_contact": "' + b"x" * 5000 + b'"}'])
def test_unusable_file_falls_back_to_settings(tmp_path, fake_plaid, content):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / sc.FILE_NAME).write_bytes(content)
    app = create_app(make_settings(tmp_path, support_contact="From settings"), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert status(c)["support_contact"] == "From settings"


def test_file_values_are_cleaned_like_the_settings_file(tmp_path):
    settings = make_settings(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / sc.FILE_NAME).write_text(
        json.dumps({"support_contact": "  Sa\u0000m\n" + "y" * 100}), encoding="utf-8")
    got = sc.read_saved(settings)
    assert got is not None and got.startswith("Sam") and len(got) <= SUPPORT_CONTACT_MAX
    assert all(ch.isprintable() for ch in got)


@pytest.mark.parametrize("body", [
    {"contact": "x" * (SUPPORT_CONTACT_MAX + 1)},
    {"contact": "Sam\n555"},
    {"contact": "Sam\u0007"},
    {"contact": "Sam‮"},  # invisible direction override
    {"contact": 5},
    {"contact": None},
    {},
    {"contact": "Sam", "extra": 1},
    {"contact": "y" * 201},
])
def test_bad_values_are_refused_without_echo(unlocked_app, tmp_path, body):
    app, c = unlocked_app
    r = c.put(ROUTE, json=body)
    assert r.status_code == 422
    if isinstance(body.get("contact"), str) and len(body["contact"]) > 3:
        assert body["contact"] not in r.text
    assert not (tmp_path / "data" / sc.FILE_NAME).exists()
    assert status(c)["support_contact"] == "From settings"


def test_exactly_the_limit_and_trimmed_spaces_are_fine(unlocked_app):
    app, c = unlocked_app
    name = "S" * SUPPORT_CONTACT_MAX
    assert put(c, f"  {name}  ").json() == {"support_contact": name}


def test_needs_a_session_and_the_csrf_header(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert put(c, "Sam").status_code == 401  # not set up / locked
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        c.post("/api/auth/lock")
        assert put(c, "Sam").status_code == 401
    with SessionClient(app, base_url=BASE_URL) as bare:
        assert bare.put(ROUTE, json={"contact": "Sam"}).status_code in (401, 403)
    assert not (tmp_path / "data" / sc.FILE_NAME).exists()


def test_update_status_shows_the_same_name(unlocked_app):
    app, c = unlocked_app
    put(c, "Sam")
    assert app.state.fintrack.settings.support_contact == "Sam"
