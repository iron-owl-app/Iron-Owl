"""Release 3.4: Plaid keys saved in the vault (Settings -> Bank connection).

No real Plaid keys: every check goes through ``FakeKeyFactory`` (``create_app(plaid_factory=...)``)
over the full HTTP stack, and the real client wrapper is tested with ``client._api`` patched.
"""
from __future__ import annotations

import datetime as dt
import logging

import pytest

from app.main import create_app
from app.models import AppSetting, PlaidItem
from app.plaid_client import CHECK_TIMEOUT, PlaidClient, PlaidError, token_env
from app.services import automation
from app.services import plaid_keys as keys_service
from app.utils import utcnow
from tests.conftest import BASE_URL, CSRF, PASSWORD, FakeKeyFactory, SessionClient, make_settings
from tests.test_backup import download, restore

CID = "a" * 24
SECRET = "b" * 30
CID2 = "c" * 24
SECRET2 = "e" * 29 + "Z"
WRONG = "not my password at all"

SAVE_PASSWORD_MISSING = "Enter your current Iron Owl password to change your Plaid keys."
WRONG_PASSWORD = "Your current Iron Owl password is incorrect."


def keys(client_id=CID, secret=SECRET, env="sandbox") -> dict:
    return {"client_id": client_id, "secret": secret, "env": env}


def save(c, password=PASSWORD, **kw):
    return c.put("/api/plaid/keys", json={**keys(**kw), "current_password": password})


def remove(c, password=PASSWORD):
    return c.post("/api/plaid/keys/delete", json={"current_password": password})


def rows(state) -> dict[str, str | None]:
    session = state.vault.db.acquire()
    try:
        found = {r.key: r.value for r in session.query(AppSetting) if r.key.startswith("plaid_keys")}
        session.commit()
        return found
    finally:
        state.vault.db.release(session)


def add_item(state, token="access-sandbox-x", status="ok") -> int:
    session = state.vault.db.acquire()
    try:
        item = PlaidItem(plaid_item_id=f"item-{token}", access_token=token, kind="bank", status=status)
        session.add(item)
        session.commit()
        return item.id
    finally:
        state.vault.db.release(session)


class Keys:
    """A FinTrack instance with no .env keys and a scripted key factory."""

    def __init__(self, c, factory: FakeKeyFactory, clock) -> None:
        self.c, self.factory, self.clock = c, factory, clock
        self.state = c.app.state.fintrack

    def test(self, body=None):
        self.clock.advance(3)  # past the 2s throttle
        return self.c.post("/api/plaid/keys/test", json=body if body is not None else {})

    def get(self) -> dict:
        r = self.c.get("/api/plaid/keys")
        assert r.status_code == 200, r.text
        return r.json()


def make_keys_client(tmp_path, factory, clock, **settings):
    app = create_app(make_settings(tmp_path, **settings), plaid_factory=factory)
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    c.__enter__()
    assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return Keys(c, factory, clock)


@pytest.fixture
def k(tmp_path, key_factory, clock):
    kc = make_keys_client(tmp_path, key_factory, clock)
    yield kc
    kc.c.__exit__(None, None, None)


# ------------------------------------------------------------------ 1. nothing set


def test_status_none(k):
    assert k.get() == {
        "source": "none", "client_id": None, "env": None, "secret_hint": None, "saved_at": None,
        "last_test": None, "vault_keys_ignored": False, "env_file_partial": False, "linked_items": 0,
    }
    assert k.c.get("/api/plaid/status").json() == {"configured": False, "env": "sandbox", "source": "none", "items_cap": False, "items_linked": 0, "items_limit": 0, "items_now": 0}
    assert k.c.post("/api/plaid/link-token", json={"kind": "bank"}).status_code == 503


# ------------------------------------------------------------------ 2-6. POST /test


def test_candidate_keys_ok_store_nothing(k):
    before = k.state.plaid
    r = k.test(keys())
    assert r.status_code == 200
    body = r.json()
    assert body["at"].endswith("Z")
    assert {**body, "at": None} == {
        "ok": True, "code": "ok", "message": "These keys work — Test banks (Sandbox).",
        "plaid_code": None, "at": None, "env": "sandbox", "checked_linked_items": False,
    }
    assert k.get()["source"] == "none"
    assert rows(k.state) == {}
    assert k.state.plaid_keys.client is None and k.state.plaid is before
    assert k.factory.calls == [(CID, SECRET, "sandbox")]
    assert k.test(keys(env="production")).json()["message"] == "These keys work — Real banks (Production)."


@pytest.mark.parametrize(
    "secret, env, code, plaid_code",
    [
        ("bad" + "b" * 27, "sandbox", "invalid_keys", "INVALID_API_KEYS"),
        ("net" + "b" * 27, "sandbox", "unreachable", "NETWORK_ERROR"),
        ("unauth" + "b" * 24, "production", "wrong_environment", "UNAUTHORIZED_ENVIRONMENT"),
        ("weird" + "b" * 25, "sandbox", "plaid_error", "UNKNOWN"),
        ("rate" + "b" * 26, "sandbox", "plaid_error", "RATE_LIMIT_EXCEEDED"),
    ],
)
def test_candidate_keys_failures(k, secret, env, code, plaid_code):
    body = k.test(keys(secret=secret, env=env)).json()
    assert (body["ok"], body["code"], body["plaid_code"], body["env"]) == (False, code, plaid_code, env)
    assert "fake" not in body["message"] and secret not in body["message"]  # server text only
    assert plaid_code not in body["message"]  # codes go in plaid_code, not the main text
    assert k.get()["source"] == "none"


def test_invalid_keys_message_names_the_env(k):
    body = k.test(keys(secret="bad" + "b" * 27, env="production")).json()
    assert "Production secret" in body["message"]


def test_test_active_keys(k):
    r = k.test()
    assert r.status_code == 409
    assert r.json() == {"detail": "No Plaid keys are set yet.", "code": "not_set"}
    assert k.factory.calls == []

    assert save(k.c).status_code == 200
    k.factory.forced["sandbox"] = ("API_ERROR", "INTERNAL_SERVER_ERROR")
    body = k.test().json()
    assert (body["ok"], body["code"], body["plaid_code"]) == (False, "plaid_error", "INTERNAL_SERVER_ERROR")
    assert k.factory.calls[-1] == (CID, SECRET, "sandbox")
    last = k.get()["last_test"]  # {} on the vault's keys records the result
    assert last == {k2: body[k2] for k2 in ("ok", "code", "message", "plaid_code", "at", "env")}
    k.factory.forced.clear()
    assert k.test().json()["ok"] is True
    assert k.get()["last_test"]["ok"] is True


def test_test_active_env_keys_does_not_persist(tmp_path, key_factory, clock):
    kc = make_keys_client(tmp_path, key_factory, clock, plaid_client_id=CID2, plaid_secret=SECRET2)
    body = kc.test().json()
    assert body["ok"] is True and body["env"] == "sandbox"
    assert key_factory.calls == [(CID2, SECRET2, "sandbox")]
    assert rows(kc.state) == {}
    assert kc.get()["last_test"] is None
    kc.c.__exit__(None, None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"client_id": CID},
        {"client_id": CID, "secret": SECRET},
        {"secret": SECRET, "env": "sandbox"},
        {**keys(), "env": "development"},
        {**keys(), "env": None, "client_id": None},
        {**keys(), "extra": 1},
        {**keys(), "client_id": "short"},
        {**keys(), "client_id": "g" * 65},
        {**keys(), "secret": "b" * 15},
        {**keys(), "secret": "b" * 29 + "!"},
        {**keys(), "secret": 12345678901234567890},
        {**keys(), "current_password": PASSWORD},
    ],
)
def test_test_validation(k, body):
    r = k.test(body)
    assert r.status_code == 422, r.text
    for value in body.values():
        if isinstance(value, str) and value not in ("sandbox", "development", "client_id"):
            assert value not in r.text
    assert k.factory.calls == []


def test_test_partial_body_message(k):
    r = k.test({"client_id": CID})
    assert "send client_id, secret and env together, or none of them" in r.json()["detail"]


def test_keys_are_trimmed(k):
    assert k.test(keys(client_id=f"  {CID}\n", secret=f"\t{SECRET} \r\n")).json()["ok"] is True
    assert k.factory.calls == [(CID, SECRET, "sandbox")]


def test_throttle(k):
    assert k.test(keys()).status_code == 200
    r = k.c.post("/api/plaid/keys/test", json=keys())  # no clock advance
    assert r.status_code == 429
    assert r.json() == {"detail": "Wait a moment before testing again.", "retry_after": 2}
    assert r.headers["retry-after"] == "2"
    k.clock.advance(1)
    assert k.c.post("/api/plaid/keys/test", json=keys()).json()["retry_after"] == 1
    k.clock.advance(1.5)
    assert k.c.post("/api/plaid/keys/test", json=keys()).status_code == 200
    # An "auto" check tries two envs but counts as one test.
    assert k.test(keys(secret="sbx" + "b" * 27, env="auto")).json()["ok"] is True
    assert k.c.post("/api/plaid/keys/test", json=keys(env="auto")).status_code == 429
    # One at a time.
    throttle = k.state.plaid_keys.throttle
    k.clock.advance(3)
    assert throttle.start() is None
    k.clock.advance(3)
    assert k.c.post("/api/plaid/keys/test", json=keys()).status_code == 429
    throttle.done()
    assert k.c.post("/api/plaid/keys/test", json=keys()).status_code == 200


# ------------------------------------------------------------------ env "auto" (UX U2)


def test_auto_prefers_production(k):
    body = k.test(keys(env="auto")).json()
    assert (body["ok"], body["env"], body["message"]) == (True, "production", "These keys work — Real banks (Production).")
    assert k.factory.checks == ["production"]


def test_auto_falls_back_to_sandbox(k):
    body = k.test(keys(secret="sbx" + "b" * 27, env="auto")).json()
    assert (body["ok"], body["code"], body["env"]) == (True, "ok", "sandbox")
    assert k.factory.checks == ["production", "sandbox"]


@pytest.mark.parametrize(
    "secret, code, checks",
    [
        ("bad" + "b" * 27, "invalid_keys", ["production", "sandbox"]),
        ("unauth" + "b" * 24, "wrong_environment", ["production", "sandbox"]),
        ("net" + "b" * 27, "unreachable", ["production"]),  # stops at once
        ("rate" + "b" * 26, "plaid_error", ["production", "sandbox"]),
    ],
)
def test_auto_failures(k, secret, code, checks):
    body = k.test(keys(secret=secret, env="auto")).json()
    assert (body["ok"], body["code"], body["env"]) == (False, code, None)
    assert k.factory.checks == checks
    if code == "invalid_keys":
        assert body["message"] == (
            "Plaid didn’t accept this Client ID and Secret. Check that you copied both from Developers → Keys."
        )
        assert body["plaid_code"] == "INVALID_API_KEYS"


def test_auto_sandbox_unreachable_wins_over_invalid(k):
    k.factory.forced["sandbox"] = ("API_ERROR", "NETWORK_ERROR")
    body = k.test(keys(secret="bad" + "b" * 27, env="auto")).json()
    assert body["code"] == "unreachable" and k.factory.checks == ["production", "sandbox"]


def test_auto_with_items_tries_only_their_env(k):
    add_item(k.state, "access-sandbox-x")
    body = k.test(keys(env="auto")).json()
    assert (body["ok"], body["env"], body["checked_linked_items"]) == (True, "sandbox", False)
    assert k.factory.checks == ["sandbox"] and k.factory.probes == []  # candidate keys: no probe

    k.factory.checks.clear()
    body = k.test(keys(secret="prd" + "b" * 27, env="auto")).json()
    assert (body["ok"], body["code"], body["env"]) == (False, "invalid_keys", None)
    assert k.factory.checks == ["sandbox"]

    # Not owned: only the password-gated save probes, and refuses.
    r = save(k.c)
    assert r.status_code == 409 and r.json()["code"] == "items_mismatch"
    assert k.factory.probes == ["access-sandbox-x"]


def test_auto_with_mixed_items_is_a_mismatch(k):
    add_item(k.state, "access-sandbox-x")
    add_item(k.state, "access-production-y")
    body = k.test(keys(env="auto")).json()
    assert (body["code"], body["env"]) == ("items_mismatch", None)
    assert "your 2 linked institutions" in body["message"]
    assert k.factory.calls == []


def test_auto_is_only_for_test(k):
    assert save(k.c, env="auto").status_code == 422


# ------------------------------------------------------------------ 7. PUT re-auth


def test_save_requires_current_password(k, clock):
    r = k.c.put("/api/plaid/keys", json=keys())
    assert r.status_code == 422 and r.json() == {"detail": SAVE_PASSWORD_MISSING, "code": "password_required"}
    r = save(k.c, password="")
    assert r.status_code == 422 and r.json() == {"detail": SAVE_PASSWORD_MISSING, "code": "password_required"}
    for left in (4, 3, 2, 1):
        r = save(k.c, password=WRONG)
        assert r.status_code == 401
        assert r.json() == {"detail": WRONG_PASSWORD, "code": "wrong_password", "tries_left": left}
    r = save(k.c, password=WRONG)
    assert r.status_code == 429 and r.json()["retry_after"] == 30
    assert r.headers["retry-after"] == "30"
    assert save(k.c).status_code == 429  # even the right password waits
    assert k.factory.calls == []  # a wrong password never reaches Plaid
    # The limiter is shared with unlock.
    k.c.post("/api/auth/lock")
    assert k.c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 429
    clock.advance(31)
    assert k.c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert save(k.c).status_code == 200


# ------------------------------------------------------------------ 8. PUT success


def test_save_success(k):
    r = save(k.c)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved_at"].endswith("Z")
    assert body["last_test"]["ok"] is True and body["last_test"]["env"] == "sandbox"
    assert {**body, "saved_at": None, "last_test": None} == {
        "source": "vault", "client_id": CID, "env": "sandbox", "secret_hint": SECRET[-4:], "saved_at": None,
        "last_test": None, "vault_keys_ignored": False, "env_file_partial": False, "linked_items": 0,
    }
    assert k.get() == body
    assert k.c.get("/api/plaid/status").json() == {"configured": True, "env": "sandbox", "source": "vault", "items_cap": False, "items_linked": 0, "items_limit": 0, "items_now": 0}
    vault_client = k.state.plaid_keys.client
    assert k.factory.calls[-1] == (CID, SECRET, "sandbox") and vault_client is k.factory.clients[-1]
    assert k.state.active_plaid() is vault_client
    r = k.c.post("/api/plaid/link-token", json={"kind": "bank"})
    assert r.status_code == 200 and ("link", "bank", None) in vault_client.calls


def test_auto_sync_uses_the_vault_client(k):
    assert save(k.c).status_code == 200
    assert k.state.plaid.configured is False  # the real, unconfigured .env client
    add_item(k.state, "access-sandbox-x")  # never synced: due at once
    assert automation.sync_tick(k.state, now=utcnow() + dt.timedelta(minutes=1)) is True
    assert any(call[1] == "access-sandbox-x" for call in k.state.plaid_keys.client.calls)


# ------------------------------------------------------------------ 9. PUT with bad keys


@pytest.mark.parametrize(
    "secret, env, status, code",
    [
        ("bad" + "b" * 27, "sandbox", 422, "invalid_keys"),
        ("unauth" + "b" * 24, "production", 422, "wrong_environment"),
        ("net" + "b" * 27, "sandbox", 502, "unreachable"),
        ("rate" + "b" * 26, "sandbox", 502, "plaid_error"),
    ],
)
def test_save_bad_keys_stores_nothing(k, secret, env, status, code):
    r = save(k.c, secret=secret, env=env)
    assert r.status_code == status
    body = r.json()
    assert body["code"] == code and body["plaid_code"] and isinstance(body["detail"], str)
    assert rows(k.state) == {} and k.state.plaid_keys.client is None


def test_save_bad_keys_keeps_the_saved_ones(k):
    assert save(k.c).status_code == 200
    client, saved = k.state.plaid_keys.client, rows(k.state)
    r = save(k.c, client_id=CID2, secret="bad" + "b" * 27)
    assert r.status_code == 422 and r.json()["code"] == "invalid_keys"
    assert r.json()["plaid_code"] == "INVALID_API_KEYS"
    assert rows(k.state) == saved and k.state.plaid_keys.client is client
    assert k.get()["client_id"] == CID


# ------------------------------------------------------------------ 10. the secret never leaks


def _leak_check_all(k, secret, caplog):
    texts = []

    def keep(r):
        texts.append(r.text)
        texts.append(str(r.headers))
        return r

    keep(k.c.get("/api/plaid/keys"))
    keep(k.test(keys(secret=secret)))
    keep(k.test(keys(secret=secret, env="auto")))
    keep(k.test(keys(secret=secret, env="production")))
    keep(save(k.c, secret=secret))
    keep(k.c.get("/api/plaid/keys"))
    keep(k.test())
    keep(k.c.get("/api/plaid/status"))
    keep(save(k.c, password=WRONG, secret=secret))
    keep(k.c.post("/api/plaid/keys/test", json=keys(secret=secret)))  # throttled
    keep(k.test({**keys(secret=secret), "env": "nope"}))
    keep(k.test({"secret": secret}))
    keep(k.c.post("/api/auth/lock"))
    assert k.c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    keep(k.c.get("/api/plaid/keys"))
    keep(remove(k.c))
    return texts


LEAKY = "LeakCanary0123456789abcdefQRST"


def test_secret_never_leaks(k, caplog):
    caplog.set_level(logging.DEBUG)
    texts = _leak_check_all(k, LEAKY, caplog)
    add_item(k.state, "access-sandbox-x")
    texts.append(save(k.c, secret=LEAKY).text)  # items_mismatch (not owned)
    texts.append(save(k.c, secret="bad" + LEAKY).text)  # Plaid's error_message holds the secret
    texts.append(k.test(keys(secret="bad" + LEAKY, env="auto")).text)
    for text in texts:
        assert LEAKY not in text
    assert LEAKY not in caplog.text
    assert any("plaid keys saved (env=sandbox)" in r.getMessage() for r in caplog.records)
    assert all(LEAKY not in repr(client) for client in k.factory.clients)
    assert LEAKY not in repr(k.state)
    stored = keys_service.Keys(CID, LEAKY, "sandbox")
    assert LEAKY not in repr(stored) and LEAKY not in str(stored)


def test_secret_never_leaks_through_the_real_client(tmp_path, clock, caplog, monkeypatch):
    """The real PlaidClient wrapper, with the SDK call failing in ways that carry the secret."""
    import plaid

    mode = {"fail": "api"}

    def factory(client_id, secret, env):
        client = PlaidClient.from_keys(client_id, secret, env)

        def institutions_get(_request, **_kwargs):
            if mode["fail"] == "network":
                raise ConnectionError(f"failed sending PLAID-SECRET: {secret}")
            exc = plaid.ApiException(status=400, reason=f"Bad Request {secret}")
            exc.body = (
                '{"error_type":"INVALID_INPUT","error_code":"INVALID_API_KEYS",'
                f'"error_message":"invalid secret {secret}"}}'
            )
            raise exc

        monkeypatch.setattr(client._api, "institutions_get", institutions_get)
        return client

    caplog.set_level(logging.DEBUG)
    kc = make_keys_client(tmp_path, factory, clock)
    texts = []
    for fail in ("api", "network"):
        mode["fail"] = fail
        texts += [kc.test(keys(secret=LEAKY)).text, kc.test(keys(secret=LEAKY, env="auto")).text]
        texts.append(save(kc.c, secret=LEAKY).text)
    assert kc.test(keys(secret=LEAKY)).json()["code"] == "unreachable"
    mode["fail"] = "api"
    assert kc.test(keys(secret=LEAKY)).json()["code"] == "invalid_keys"
    for text in texts:
        assert LEAKY not in text
    assert LEAKY not in caplog.text
    kc.c.__exit__(None, None, None)


def test_a_failing_factory_leaks_nothing(tmp_path, clock, caplog):
    def factory(client_id, secret, env):
        raise RuntimeError(f"cannot build a client for {secret}")

    caplog.set_level(logging.DEBUG)
    kc = make_keys_client(tmp_path, factory, clock)
    r = kc.test(keys(secret=LEAKY))
    assert r.json()["code"] == "plaid_error" and r.json()["plaid_code"] == "UNKNOWN"
    # Saved directly (the factory can't pass a check): reload fails and leaves no client.
    session = kc.state.vault.db.acquire()
    try:
        keys_service.write_keys(session, CID, LEAKY, "sandbox", {"ok": True, "code": "ok", "message": "m",
                                                                  "plaid_code": None, "at": "x", "env": "sandbox"})
        session.commit()
    finally:
        kc.state.vault.db.release(session)
    kc.state.plaid_keys.reload(kc.state)
    assert kc.state.plaid_keys.client is None
    assert LEAKY not in r.text and LEAKY not in caplog.text
    assert "RuntimeError" in caplog.text
    kc.c.__exit__(None, None, None)


# ------------------------------------------------------------------ 11. .env precedence


def test_env_keys_take_precedence(tmp_path, key_factory, clock):
    kc = make_keys_client(tmp_path, key_factory, clock, plaid_client_id=CID2, plaid_secret=SECRET2)
    state = kc.state
    status = kc.get()
    assert {k2: status[k2] for k2 in ("source", "client_id", "env", "secret_hint", "saved_at", "last_test")} == {
        "source": "env", "client_id": CID2, "env": "sandbox", "secret_hint": SECRET2[-4:],
        "saved_at": None, "last_test": None,
    }
    assert status["vault_keys_ignored"] is False and status["env_file_partial"] is False
    assert kc.c.get("/api/plaid/status").json() == {"configured": True, "env": "sandbox", "source": "env", "items_cap": False, "items_linked": 0, "items_limit": 0, "items_now": 0}

    r = save(kc.c, password=WRONG)
    assert r.status_code == 409 and r.json()["code"] == "managed_by_env"
    assert "PLAID_CLIENT_ID" in r.json()["detail"]
    assert state.vault.limiter.failures == 0  # checked before the password: nothing counted
    assert key_factory.calls == []
    assert state.active_plaid() is state.plaid

    # Rows left from before the .env keys: ignored (never loaded), and removable.
    session = state.vault.db.acquire()
    try:
        keys_service.write_keys(session, CID, SECRET, "sandbox", {"ok": True, "code": "ok", "message": "m",
                                                                   "plaid_code": None, "at": "x", "env": "sandbox"})
        session.commit()
    finally:
        state.vault.db.release(session)
    kc.c.post("/api/auth/lock")
    assert kc.c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert state.plaid_keys.client is None and key_factory.calls == []
    assert kc.get()["vault_keys_ignored"] is True and kc.get()["source"] == "env"
    assert remove(kc.c).status_code == 204
    assert kc.get()["vault_keys_ignored"] is False and rows(state) == {}
    kc.c.__exit__(None, None, None)


def test_short_env_secret_has_no_hint(tmp_path, key_factory, clock):
    kc = make_keys_client(tmp_path, key_factory, clock, plaid_client_id="cid", plaid_secret="sec")
    assert kc.get()["source"] == "env" and kc.get()["secret_hint"] is None
    kc.c.__exit__(None, None, None)


@pytest.mark.parametrize("partial", [{"plaid_client_id": CID2}, {"plaid_secret": SECRET2}])
def test_partial_env_is_ignored(tmp_path, key_factory, clock, partial):
    kc = make_keys_client(tmp_path, key_factory, clock, **partial)
    status = kc.get()
    assert (status["source"], status["env_file_partial"]) == ("none", True)
    assert save(kc.c).status_code == 200
    assert kc.get()["source"] == "vault" and kc.state.plaid_keys.client is not None
    kc.c.__exit__(None, None, None)


# ------------------------------------------------------------------ 12. lock / unlock


def test_lock_unlock_lifecycle(k, clock):
    assert save(k.c).status_code == 200
    assert k.state.plaid_keys.client is not None
    k.c.post("/api/auth/lock")
    assert k.state.plaid_keys.client is None
    assert k.state.active_plaid().configured is False
    k.state.plaid_keys.reload(k.state)  # while locked: stays empty
    assert k.state.plaid_keys.client is None

    n = len(k.factory.calls)
    assert k.c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert len(k.factory.calls) == n + 1 and k.factory.calls[-1] == (CID, SECRET, "sandbox")
    assert k.state.active_plaid() is k.factory.clients[-1]
    assert k.c.get("/api/plaid/status").json()["source"] == "vault"

    clock.advance(16 * 60)
    k.state.vault.check_idle()
    assert not k.state.vault.unlocked and k.state.plaid_keys.client is None


def test_hook_failures_never_block(k, caplog):
    assert save(k.c).status_code == 200

    def boom():
        raise RuntimeError("boom")

    k.state.vault.after_lock = boom
    k.c.post("/api/auth/lock")
    assert not k.state.vault.unlocked
    k.state.vault.after_unlock = boom
    assert k.c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert "post-lock task failed: RuntimeError" in caplog.text
    assert "post-unlock task failed: RuntimeError" in caplog.text


def test_change_password_keeps_the_keys(k):
    assert save(k.c).status_code == 200
    new = "an even better passphrase"
    r = k.c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": new})
    assert r.status_code == 200
    assert k.state.plaid_keys.client is not None and k.get()["source"] == "vault"


def test_change_password_reopen_failure_drops_the_client(k, monkeypatch):
    assert save(k.c).status_code == 200
    vault = k.state.vault

    def reopen_fails(*_args, **_kwargs):
        raise RuntimeError("cannot reopen")

    monkeypatch.setattr(vault.db, "open", reopen_fails)
    new = "an even better passphrase"
    with pytest.raises(RuntimeError):
        vault.change_password(PASSWORD, new)
    assert not vault.unlocked and k.state.plaid_keys.client is None  # after-lock hook ran
    monkeypatch.undo()
    assert k.c.post("/api/auth/unlock", json={"password": new}).status_code == 200  # the rekey stuck
    assert k.state.plaid_keys.client is not None


def test_reload_swaps_without_a_gap(k):
    assert save(k.c).status_code == 200
    store = k.state.plaid_keys
    old = store.client
    seen = []

    def factory(*args):
        seen.append(store.client)  # still the old client while the new one is built
        return k.factory(*args)

    store._factory = factory
    store.reload(k.state)
    assert seen == [old] and store.client is k.factory.clients[-1] and store.client is not old

    def broken(*_args):
        raise RuntimeError("no")

    store._factory = broken
    store.reload(k.state)
    assert store.client is None  # a failed build leaves no client


# ------------------------------------------------------------------ 13-14. backup and restore


def test_restore_reactivates_the_backed_up_keys(k, tmp_path, clock):
    assert save(k.c).status_code == 200
    backup = download(k.c)
    assert backup.status_code == 200
    assert SECRET.encode() not in backup.content  # 14: the backup is encrypted
    assert save(k.c, client_id=CID2, secret=SECRET2).status_code == 200
    assert k.get()["secret_hint"] == SECRET2[-4:]

    r = restore(k.c, backup.content, current_password=PASSWORD)
    assert r.status_code == 200, r.text
    assert k.get()["secret_hint"] == SECRET[-4:]
    assert k.factory.calls[-1] == (CID, SECRET, "sandbox")
    assert k.state.active_plaid() is k.factory.clients[-1]

    other_factory = FakeKeyFactory()
    app = create_app(make_settings(tmp_path / "other"), plaid_factory=other_factory)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as fresh:
        assert restore(fresh, backup.content).status_code == 200
        assert other_factory.calls == [(CID, SECRET, "sandbox")]
        assert fresh.get("/api/plaid/keys").json()["source"] == "vault"
        assert fresh.get("/api/plaid/status").json() == {"configured": True, "env": "sandbox", "source": "vault", "items_cap": False, "items_linked": 0, "items_limit": 0, "items_now": 0}


# ------------------------------------------------------------------ 15. linked Items


def test_items_rules(k):
    add_item(k.state, "access-sandbox-x", status="pending")  # pending counts
    assert k.get()["linked_items"] == 1

    r = save(k.c, env="production")
    assert r.status_code == 409 and r.json()["code"] == "items_mismatch"
    assert "plaid_code" not in r.json()
    assert "your linked institution" in r.json()["detail"]
    assert k.factory.calls == []  # the token's env decides locally

    r = save(k.c)
    assert r.status_code == 409 and r.json()["code"] == "items_mismatch"
    assert k.factory.probes == ["access-sandbox-x"] and rows(k.state) == {}

    k.factory.owned = {"access-sandbox-x"}  # e.g. a rotated secret of the same account
    r = save(k.c)
    assert r.status_code == 200 and r.json()["linked_items"] == 1
    probes = len(k.factory.probes)
    body = k.test().json()  # the saved keys: probed
    assert body["ok"] is True and body["checked_linked_items"] is True
    assert len(k.factory.probes) == probes + 1
    body = k.test(keys(secret=SECRET2)).json()  # candidate keys: never probed
    assert body["ok"] is True and body["checked_linked_items"] is False
    assert len(k.factory.probes) == probes + 1


def test_candidate_keys_never_send_item_tokens(k):
    """/test with candidate keys runs only the local token-env check (the save probes)."""
    add_item(k.state, "access-sandbox-x")  # not owned by these keys
    body = k.test(keys()).json()
    assert {**body, "at": None} == {
        "ok": True, "code": "ok", "message": "These keys work — Test banks (Sandbox).",
        "plaid_code": None, "at": None, "env": "sandbox", "checked_linked_items": False,
    }
    body = k.test(keys(env="production")).json()  # the token's env decides locally
    assert (body["code"], body["checked_linked_items"]) == ("items_mismatch", False)
    k.factory.probe_error = PlaidError("API_ERROR", "NETWORK_ERROR", "down")
    assert k.test(keys(env="auto")).json()["ok"] is True
    assert k.factory.probes == []


def test_items_probe_errors(k):
    add_item(k.state, "access-sandbox-x")
    k.factory.probe_error = PlaidError("API_ERROR", "NETWORK_ERROR", "down")
    r = save(k.c)
    assert r.status_code == 502 and r.json()["code"] == "unreachable"
    k.factory.probe_error = None
    k.factory.owned = {"access-sandbox-x"}
    assert save(k.c).status_code == 200
    k.factory.probe_error = PlaidError("API_ERROR", "INTERNAL_SERVER_ERROR", "oops")
    body = k.test().json()
    assert (body["code"], body["plaid_code"], body["checked_linked_items"]) == (
        "plaid_error", "INTERNAL_SERVER_ERROR", True
    )


def test_save_refuses_when_items_change_during_the_check(k, monkeypatch):
    real_check = keys_service.check

    def check_then_link(*args, **kwargs):
        result = real_check(*args, **kwargs)
        add_item(k.state, "access-sandbox-new")  # linked while Plaid was being asked
        return result

    monkeypatch.setattr(keys_service, "check", check_then_link)
    r = save(k.c)
    assert r.status_code == 409
    assert r.json() == {
        "detail": "Your linked banks changed while Iron Owl was checking these keys. Please try again.",
        "code": "items_changed",
    }
    assert rows(k.state) == {} and k.state.plaid_keys.client is None
    monkeypatch.setattr(keys_service, "check", real_check)
    k.factory.owned = {"access-sandbox-new"}
    assert save(k.c).status_code == 200  # nothing changed this time


# ------------------------------------------------------------------ 16. remove


def test_remove(k):
    assert save(k.c).status_code == 200
    r = remove(k.c, password=WRONG)
    assert r.status_code == 401
    assert r.json() == {"detail": WRONG_PASSWORD, "code": "wrong_password", "tries_left": 4}
    r = remove(k.c, password="")
    assert r.status_code == 422
    assert r.json() == {
        "detail": "Enter your current Iron Owl password to remove your Plaid keys.", "code": "password_required",
    }
    assert k.state.plaid_keys.client is not None

    r = remove(k.c)
    assert r.status_code == 204 and r.content == b""
    assert k.get()["source"] == "none" and rows(k.state) == {}
    assert k.state.plaid_keys.client is None
    assert k.c.get("/api/plaid/status").json()["configured"] is False
    assert remove(k.c).status_code == 204  # idempotent


def test_remove_with_items_then_delete_item_locally(k):
    item_id = add_item(k.state, "access-sandbox-x")
    k.factory.owned = {"access-sandbox-x"}
    assert save(k.c).status_code == 200
    vault_client = k.state.plaid_keys.client
    assert remove(k.c).status_code == 204
    assert k.c.delete(f"/api/plaid/items/{item_id}").status_code == 204
    assert vault_client.removed == []  # no keys: removed locally only
    assert k.get()["linked_items"] == 0


def test_locked_endpoints(k):
    k.c.post("/api/auth/lock")
    assert k.c.get("/api/plaid/keys").status_code == 401
    assert k.c.post("/api/plaid/keys/test", json={}).status_code == 401
    assert save(k.c).status_code == 401
    assert remove(k.c).status_code == 401


def test_malformed_rows_mean_no_keys(k, caplog):
    assert save(k.c).status_code == 200
    session = k.state.vault.db.acquire()
    try:
        session.get(AppSetting, keys_service.META_KEY).value = '{"v": 2, "client_id": "LeakCanaryId"}'
        session.get(AppSetting, keys_service.LAST_TEST_KEY).value = "not json"
        session.commit()
    finally:
        k.state.vault.db.release(session)
    assert k.get()["source"] == "none"
    k.state.plaid_keys.reload(k.state)
    assert k.state.plaid_keys.client is None
    assert "LeakCanaryId" not in caplog.text
    assert remove(k.c).status_code == 204 and rows(k.state) == {}


# ------------------------------------------------------------------ 18. the real client wrapper


def test_real_client_from_keys(monkeypatch):
    import plaid

    client = PlaidClient.from_keys(CID, LEAKY, "sandbox")
    assert (client.configured, client.env, client.source) == (True, "sandbox", "vault")
    assert repr(client) == "<PlaidClient env=sandbox configured=True source=vault>"
    assert LEAKY not in repr(client) and CID not in repr(client)
    assert client._api.api_client.configuration.debug is False

    seen = {}

    def institutions_get(request, **kwargs):
        seen["kwargs"] = kwargs
        seen["count"] = request.count
        exc = plaid.ApiException(status=400, reason="Bad Request")
        exc.body = '{"error_type":"INVALID_INPUT","error_code":"INVALID_API_KEYS","error_message":"m"}'
        raise exc

    monkeypatch.setattr(client._api, "institutions_get", institutions_get)
    with pytest.raises(PlaidError) as info:
        client.check_keys()
    assert info.value.error_code == "INVALID_API_KEYS"
    assert seen == {"kwargs": {"_request_timeout": CHECK_TIMEOUT}, "count": 1}

    def network_down(_request, **_kwargs):
        raise ConnectionError(f"failed sending {LEAKY}")

    monkeypatch.setattr(client._api, "institutions_get", network_down)
    with pytest.raises(PlaidError) as info:
        client.check_keys()
    assert info.value.error_code == "NETWORK_ERROR"
    assert LEAKY not in str(info.value) and LEAKY not in info.value.error_message

    env_client = PlaidClient(make_settings_for_env())
    assert (env_client.source, env_client.configured) == ("env", True)
    none_client = PlaidClient(make_settings_for_env(plaid_secret=""))
    assert (none_client.source, none_client.configured) == ("none", False)


def make_settings_for_env(**overrides):
    import pathlib

    values = {"plaid_client_id": CID2, "plaid_secret": SECRET2, **overrides}
    return make_settings(pathlib.Path("unused"), **values)


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (None, True),
        (("ITEM_ERROR", "ITEM_LOGIN_REQUIRED"), True),
        (("ITEM_ERROR", "PENDING_EXPIRATION"), True),
        (("ITEM_ERROR", "NO_ACCOUNTS"), True),
        (("ITEM_ERROR", "ITEM_NOT_FOUND"), False),  # not under these keys
        (("ITEM_ERROR", "SOMETHING_NEW"), False),  # unknown: not proof of ownership
        (("INVALID_INPUT", "INVALID_ACCESS_TOKEN"), False),
        (("API_ERROR", "INTERNAL_SERVER_ERROR"), PlaidError),
    ],
)
def test_real_client_owns_item(monkeypatch, outcome, expected):
    import plaid

    client = PlaidClient.from_keys(CID, SECRET, "production")
    seen = {}

    def item_get(request, **kwargs):
        seen["token"], seen["kwargs"] = request.access_token, kwargs
        if outcome is None:
            class Resp:
                def to_dict(self):
                    return {"item": {}}

            return Resp()
        exc = plaid.ApiException(status=400, reason="Bad Request")
        exc.body = f'{{"error_type":"{outcome[0]}","error_code":"{outcome[1]}","error_message":"m"}}'
        raise exc

    monkeypatch.setattr(client._api, "item_get", item_get)
    if expected is PlaidError:
        with pytest.raises(PlaidError):
            client.owns_item("access-production-x")
    else:
        assert client.owns_item("access-production-x") is expected
    assert seen == {"token": "access-production-x", "kwargs": {"_request_timeout": CHECK_TIMEOUT}}


def test_token_env():
    assert token_env("access-sandbox-1234") == "sandbox"
    assert token_env("access-production-1234") == "production"
    assert token_env("access-development-1234") == "development"
    assert token_env("public-sandbox-1234") is None
    assert token_env("xaccess-sandbox-1") is None
    assert token_env("") is None
