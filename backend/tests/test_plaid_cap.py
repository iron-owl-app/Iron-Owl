"""Iron Owl 2.0.0 (SPEC "M3. Bank connection limit is a setting"): Plaid's Trial limit of 10
bank connections is a vault setting (``app_settings.plaid_items_cap``). A vault that never saved
it decides once: on when it already has a bank connection or a saved count, off otherwise."""
from __future__ import annotations

from app.models import AppSetting, PlaidItem
from app.security import SESSION_HEADER
from tests.conftest import PASSWORD
from tests.test_plaid import api, exchange, plaid_setup  # noqa: F401  (fixtures)

CAP = "plaid_items_cap"


def _status(api) -> dict:
    r = api.get("/api/plaid/status")
    assert r.status_code == 200, r.text
    return r.json()


def _setting(vault, key: str) -> str | None:
    s = vault.db.acquire()
    try:
        row = s.get(AppSetting, key)
        return None if row is None else row.value
    finally:
        vault.db.release(s)


def _put_setting(vault, key: str, value: str | None) -> None:
    s = vault.db.acquire()
    try:
        row = s.get(AppSetting, key)
        if value is None:
            if row is not None:
                s.delete(row)
        elif row is None:
            s.add(AppSetting(key=key, value=value))
        else:
            row.value = value
        s.commit()
    finally:
        vault.db.release(s)


def _add_old_items(vault, n: int) -> None:
    s = vault.db.acquire()
    try:
        s.add_all([PlaidItem(plaid_item_id=f"old{i}", access_token=f"t{i}", kind="bank", status="ok") for i in range(n)])
        s.commit()
    finally:
        vault.db.release(s)


def _relock(api) -> None:
    assert api.post("/api/auth/lock", json={}).status_code == 200
    assert api.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200


def test_new_vault_starts_with_the_limit_off(api, vault):
    body = _status(api)
    assert body["items_cap"] is False and body["items_limit"] == 0 and body["items_linked"] == 0
    assert _setting(vault, CAP) is None  # a GET doesn't write
    _relock(api)
    assert _setting(vault, CAP) == "0"  # decided once, on unlock
    assert _status(api)["items_cap"] is False


def test_new_vault_first_connection_keeps_the_limit_off(api, vault):
    """The choice is saved before the first new Item is counted, so it can't flip to on."""
    exchange(api, "public-bank", "bank")
    assert _setting(vault, CAP) == "0"
    _relock(api)
    body = _status(api)
    assert body["items_cap"] is False and body["items_limit"] == 0
    assert body["items_linked"] == 1  # the lifetime count goes up all the same


def test_old_vault_with_a_bank_connection_turns_the_limit_on_once(api, vault):
    _add_old_items(vault, 2)
    assert _status(api)["items_cap"] is True  # before it's saved: what it would save
    _relock(api)
    assert _setting(vault, CAP) == "1"
    body = _status(api)
    assert body == {"configured": True, "env": "sandbox", "source": "env", "items_cap": True,
                    "items_linked": 2, "items_limit": 10, "items_now": 2}
    # Decided once: turning it off sticks across unlocks, connections or not.
    assert api.put("/api/plaid/items-cap", json={"on": False}).status_code == 200
    _relock(api)
    assert _setting(vault, CAP) == "0" and _status(api)["items_cap"] is False


def test_old_vault_with_only_a_saved_count_turns_the_limit_on(api, vault):
    _put_setting(vault, "plaid_items_linked", "3")  # every connection removed since
    _relock(api)
    assert _setting(vault, CAP) == "1"
    body = _status(api)
    assert body["items_cap"] is True and body["items_limit"] == 10 and body["items_linked"] == 3


def test_a_zero_or_unreadable_count_alone_keeps_it_off(api, vault):
    for raw in ("0", "junk"):
        _put_setting(vault, CAP, None)
        _put_setting(vault, "plaid_items_linked", raw)
        _relock(api)
        assert _setting(vault, CAP) == "0", raw


def test_switch_on_and_off(api, vault):
    exchange(api, "public-bank", "bank")
    r = api.put("/api/plaid/items-cap", json={"on": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"items_cap": True, "items_linked": 1, "items_limit": 10, "items_now": 1}
    assert _setting(vault, CAP) == "1"
    r = api.put("/api/plaid/items-cap", json={"on": False})
    assert r.json() == {"items_cap": False, "items_linked": 1, "items_limit": 0, "items_now": 1}
    assert _status(api)["items_cap"] is False


def test_switch_body_is_strict(api, vault):
    for bad in ({}, {"on": 1}, {"on": "true"}, {"on": None}, {"on": True, "extra": 1}):
        assert api.put("/api/plaid/items-cap", json=bad).status_code == 422, bad
    assert _setting(vault, CAP) is None


def test_switch_needs_the_session_and_header(api, vault):
    assert api.put("/api/plaid/items-cap", json={"on": True}, headers={"X-FinTrack": "0"}).status_code == 403
    assert api.put("/api/plaid/items-cap", json={"on": True}, headers={SESSION_HEADER: "wrong"}).status_code == 401
    assert _setting(vault, CAP) is None


def test_count_cant_be_changed_while_the_limit_is_off(api, vault):
    exchange(api, "public-bank", "bank")
    r = api.put("/api/plaid/items-linked", json={"count": 5})
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)
    assert _status(api)["items_linked"] == 1
    assert api.put("/api/plaid/items-cap", json={"on": True}).status_code == 200
    assert api.put("/api/plaid/items-linked", json={"count": 5}).json()["items_linked"] == 5


def test_linking_isnt_blocked_past_10_while_off(api, vault):
    _put_setting(vault, CAP, "0")
    _put_setting(vault, "plaid_items_linked", "10")
    assert api.post("/api/plaid/link-token", json={"kind": "bank"}).status_code == 200
    exchange(api, "public-bank", "bank")
    body = _status(api)
    assert body["items_cap"] is False and body["items_limit"] == 0 and body["items_linked"] == 11
    # Turned on later: the lifetime count is still there.
    on = api.put("/api/plaid/items-cap", json={"on": True}).json()
    assert on["items_linked"] == 11 and on["items_limit"] == 10
