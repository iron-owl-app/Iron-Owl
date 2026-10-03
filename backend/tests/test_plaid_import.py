"""Release 2 Plaid flow: exchange -> pending + discovered accounts -> import a subset -> sync."""
from __future__ import annotations

import pytest

from tests.test_plaid import BANK_TOKEN, INV_TOKEN, LOAN_TOKEN, acct, plaid_setup, txn  # noqa: F401


@pytest.fixture
def api(unlocked, plaid_setup):  # noqa: F811
    return unlocked


def do_exchange(api, public_token="public-bank", kind="bank") -> dict:
    r = api.post("/api/plaid/exchange", json={"public_token": public_token, "kind": kind})
    assert r.status_code == 201, r.text
    return r.json()


def do_import(api, item_id, ids) -> dict:
    r = api.post(f"/api/plaid/items/{item_id}/import", json={"plaid_account_ids": ids})
    assert r.status_code == 200, r.text
    return r.json()


def test_exchange_stores_pending_item_without_syncing(api, fake_plaid):
    body = do_exchange(api, "public-loan", "loan")
    item = body["item"]
    assert item["status"] == "pending" and item["account_count"] == 0 and item["last_synced_at"] is None
    assert body["accounts"] == [
        {"plaid_account_id": "stu", "name": "Student Loan", "official_name": "Student Loan official",
         "mask": "0000", "category": "loan", "plaid_type": "loan", "plaid_subtype": "student",
         "current_balance": 25000.0, "is_liability": True, "imported": False},
        {"plaid_account_id": "mtg", "name": "Mortgage", "official_name": "Mortgage official",
         "mask": "0000", "category": "loan", "plaid_type": "loan", "plaid_subtype": "mortgage",
         "current_balance": 300000.0, "is_liability": True, "imported": False},
        {"plaid_account_id": "cc", "name": "Visa", "official_name": "Visa official",
         "mask": "0000", "category": "credit", "plaid_type": "credit", "plaid_subtype": "credit card",
         "current_balance": 1234.56, "is_liability": True, "imported": False},
    ]
    # No sync: no products endpoints called, no local accounts.
    assert not [c for c in fake_plaid.calls if c[0] in ("transactions", "liabilities", "holdings", "item_products")]
    assert api.get("/api/accounts").json() == []
    assert api.get("/api/summary").json()["items_needing_attention"] == 1  # awaiting account choice


def test_pending_items_are_skipped_by_sync(api, fake_plaid):
    item = do_exchange(api)["item"]
    assert api.post("/api/plaid/sync").json() == {"results": []}
    assert api.post("/api/plaid/sync", json={"item_id": item["id"]}).json() == {"results": []}
    assert not [c for c in fake_plaid.calls if c[0] == "transactions"]


def test_import_subset_skips_excluded_accounts(api, fake_plaid):
    item = do_exchange(api)["item"]
    body = do_import(api, item["id"], ["chk"])
    assert body["item"]["status"] == "ok" and body["item"]["account_count"] == 1
    assert body["result"] == {
        "item_id": item["id"], "institution_name": "First Bank", "ok": True, "error_code": None,
        "accounts": 1, "transactions_added": 2, "transactions_modified": 0, "transactions_removed": 0,
        "holdings": 0,
    }
    assert [a["name"] for a in api.get("/api/accounts").json()] == ["Checking"]
    # The HSA's transaction (t3) was skipped entirely.
    assert {t["name"] for t in api.get("/api/transactions").json()["items"]} == {"Coffee Shop", "Payroll"}
    listed = {a["plaid_account_id"]: a["imported"] for a in api.get(f"/api/plaid/items/{item['id']}/accounts").json()}
    assert listed == {"chk": True, "hsa_dep": False}

    # Later syncs keep skipping it.
    r = api.post("/api/plaid/sync", json={"item_id": item["id"]})
    assert r.json()["results"][0]["accounts"] == 1
    assert [a["name"] for a in api.get("/api/accounts").json()] == ["Checking"]


def test_manage_accounts_excludes_and_reincludes(api, fake_plaid):
    item = do_exchange(api)["item"]
    do_import(api, item["id"], ["chk", "hsa_dep"])
    assert api.get("/api/transactions").json()["total"] == 3

    # Exclude the HSA later: its local account (and transactions) are deleted.
    do_import(api, item["id"], ["chk"])
    assert [a["name"] for a in api.get("/api/accounts").json()] == ["Checking"]
    assert "Pharmacy" not in {t["name"] for t in api.get("/api/transactions").json()["items"]}

    # Re-include it: the cursor resets so its history is fetched again.
    calls_before = len([c for c in fake_plaid.calls if c[0] == "transactions"])
    do_import(api, item["id"], ["chk", "hsa_dep"])
    cursors = [c[2] for c in fake_plaid.calls if c[0] == "transactions"][calls_before:]
    assert cursors[0] is None
    assert "Pharmacy" in {t["name"] for t in api.get("/api/transactions").json()["items"]}
    assert {a["name"] for a in api.get("/api/accounts").json()} == {"Checking", "Health Savings"}


def test_import_validation(api, fake_plaid):
    item = do_exchange(api)["item"]
    assert api.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": []}).status_code == 422
    r = api.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["nope"]})
    assert r.status_code == 422
    assert api.post("/api/plaid/items/999/import", json={"plaid_account_ids": ["chk"]}).status_code == 404
    assert api.get("/api/plaid/items/999/accounts").status_code == 404
    # Still pending after the rejected calls.
    assert api.get("/api/plaid/items").json()[0]["status"] == "pending"


def test_discovered_accounts_errors_are_reported(api, fake_plaid):
    item = do_exchange(api)["item"]
    fake_plaid.fail(BANK_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    r = api.get(f"/api/plaid/items/{item['id']}/accounts")
    assert r.status_code == 502 and "ITEM_LOGIN_REQUIRED" in r.json()["detail"]
    assert BANK_TOKEN not in r.text


def test_exchange_keeps_item_when_accounts_fetch_fails(api, fake_plaid):
    fake_plaid.fail(INV_TOKEN, "accounts", "INTERNAL_SERVER_ERROR", error_type="API_ERROR")
    r = api.post("/api/plaid/exchange", json={"public_token": "public-inv", "kind": "investment"})
    assert r.status_code == 502
    items = api.get("/api/plaid/items").json()
    assert len(items) == 1 and items[0]["status"] == "pending"
    # The client can list the accounts again once Plaid recovers.
    assert len(api.get(f"/api/plaid/items/{items[0]['id']}/accounts").json()) == 3


def test_delete_pending_item(api, fake_plaid):
    item = do_exchange(api)["item"]
    assert api.delete(f"/api/plaid/items/{item['id']}").status_code == 204
    assert BANK_TOKEN in fake_plaid.removed
    assert api.get("/api/plaid/items").json() == []


def test_duplicate_of_pending_item_is_rejected(api, fake_plaid):
    fake_plaid.add_item("public-bank-again", "access-sandbox-bank-2", "item_bank_2", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-bank-2"] = [acct("chk2", "Checking", "depository", "checking", 5)]
    do_exchange(api)  # pending, no local accounts yet
    r = api.post("/api/plaid/exchange", json={"public_token": "public-bank-again", "kind": "bank"})
    assert r.status_code == 409
    assert "access-sandbox-bank-2" in fake_plaid.removed


def test_import_runs_rules_detection_and_alerts(api, fake_plaid):
    api.post("/api/rules", json={"field": "merchant", "op": "is", "text": "blue bottle", "action": "category",
                                 "category": "ENTERTAINMENT"})
    item = do_exchange(api)["item"]
    do_import(api, item["id"], ["chk"])
    coffee = next(t for t in api.get("/api/transactions").json()["items"] if t["name"] == "Coffee Shop")
    assert coffee["category"] == "ENTERTAINMENT" and coffee["category_source"] == "rule"
    assert coffee["plaid_category"] == "FOOD_AND_DRINK"


def test_unknown_plaid_primary_becomes_a_category(api, fake_plaid):
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("tp", "chk", 30, "Pet store", category="PET_SUPPLIES"),
                                      txn("tx", "chk", 5, "Weird", category="lower-case!")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
    ]
    item = do_exchange(api)["item"]
    do_import(api, item["id"], ["chk"])
    cats = {c["id"]: c for c in api.get("/api/categories").json()}
    assert cats["PET_SUPPLIES"]["name"] == "Pet Supplies" and cats["PET_SUPPLIES"]["kind"] == "spending"
    by_name = {t["name"]: t for t in api.get("/api/transactions").json()["items"]}
    assert by_name["Pet store"]["category"] == "PET_SUPPLIES"
    assert by_name["Weird"]["category"] is None and by_name["Weird"]["category_name"] == "Other"


def test_exchange_then_import_never_leaks_tokens(api, fake_plaid):
    bodies = []
    for public, kind in (("public-bank", "bank"), ("public-inv", "investment"), ("public-loan", "loan")):
        r = api.post("/api/plaid/exchange", json={"public_token": public, "kind": kind})
        bodies.append(r.text)
        item = r.json()["item"]
        bodies.append(api.get(f"/api/plaid/items/{item['id']}/accounts").text)
        ids = [a["plaid_account_id"] for a in r.json()["accounts"]]
        bodies.append(api.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ids}).text)
    for body in bodies:
        for token in (BANK_TOKEN, INV_TOKEN, LOAN_TOKEN):
            assert token not in body
        assert "access_token" not in body
