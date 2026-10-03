from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.plaid_client import PlaidClient, PlaidError
from app.services.categorize import categorize
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient, make_settings

BANK_TOKEN = "access-sandbox-bank-0000-SECRET"
INV_TOKEN = "access-sandbox-inv-1111-SECRET"
LOAN_TOKEN = "access-sandbox-loan-2222-SECRET"
ALL_TOKENS = (BANK_TOKEN, INV_TOKEN, LOAN_TOKEN)


def acct(account_id, name, type_, subtype, current, available=None, mask="0000"):
    return {
        "account_id": account_id,
        "name": name,
        "official_name": f"{name} official",
        "mask": mask,
        "type": type_,
        "subtype": subtype,
        "balances": {"current": current, "available": available, "iso_currency_code": "USD"},
    }


def txn(tid, account_id, amount, name, date="2026-09-01", merchant=None, category="FOOD_AND_DRINK", pending=False):
    return {
        "transaction_id": tid,
        "account_id": account_id,
        "amount": amount,
        "name": name,
        "merchant_name": merchant,
        "date": date,
        "pending": pending,
        "personal_finance_category": {"primary": category, "detailed": f"{category}_OTHER"},
    }


@pytest.fixture
def plaid_setup(fake_plaid):
    f = fake_plaid
    f.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    f.add_item("public-inv", INV_TOKEN, "item_inv", "Big Brokerage")
    f.add_item("public-loan", LOAN_TOKEN, "item_loan", "Loan Servicer")

    f.accounts[BANK_TOKEN] = [
        acct("chk", "Checking", "depository", "checking", 1000.5, 900.25),
        acct("hsa_dep", "Health Savings", "depository", "hsa", 2500),
    ]
    f.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("t1", "chk", 12.34, "Coffee Shop", merchant="Blue Bottle"),
                                      txn("t2", "chk", -500, "Payroll", category="INCOME")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": True},
        {"cursor_in": "c1", "added": [txn("t3", "hsa_dep", 40, "Pharmacy", date="2026-09-03")],
         "modified": [], "removed": [], "next_cursor": "c2", "has_more": False},
        {"cursor_in": "c2", "added": [],
         "modified": [txn("t1", "chk", 15, "Coffee Shop", merchant="Blue Bottle")],
         "removed": [{"transaction_id": "t2", "account_id": "chk"}], "next_cursor": "c3", "has_more": False},
    ]

    f.accounts[INV_TOKEN] = [
        acct("k401", "Work 401k", "investment", "401k", 50000),
        acct("brk", "Brokerage", "investment", "brokerage", 10000),
        acct("hsa_inv", "HSA Invest", "investment", "hsa", 3000),
    ]
    f.holdings[INV_TOKEN] = {
        "accounts": [acct("k401", "Work 401k", "investment", "401k", 55000.55),
                     acct("brk", "Brokerage", "investment", "brokerage", 10100)],
        "holdings": [
            {"account_id": "k401", "security_id": "s_vti", "quantity": 100.5, "institution_price": 250.1,
             "institution_value": 25135.05, "cost_basis": 20000},
            {"account_id": "k401", "security_id": "s_bnd", "quantity": 10, "institution_price": 70,
             "institution_value": 700, "cost_basis": None},
            {"account_id": "brk", "security_id": "s_cash", "quantity": 100, "institution_price": 1,
             "institution_value": 100, "cost_basis": 100},
        ],
        "securities": [
            {"security_id": "s_vti", "name": "Vanguard Total Stock Market ETF", "ticker_symbol": "VTI"},
            {"security_id": "s_bnd", "name": "Vanguard Total Bond ETF", "ticker_symbol": "BND"},
            {"security_id": "s_cash", "name": "Cash", "ticker_symbol": None},
        ],
    }

    f.accounts[LOAN_TOKEN] = [
        acct("stu", "Student Loan", "loan", "student", 25000),
        acct("mtg", "Mortgage", "loan", "mortgage", 300000),
        acct("cc", "Visa", "credit", "credit card", 1234.56),
    ]
    f.liabilities[LOAN_TOKEN] = {
        "accounts": f.accounts[LOAN_TOKEN],
        "liabilities": {
            "student": [{"account_id": "stu", "interest_rate_percentage": 5.25,
                         "minimum_payment_amount": 280.12, "next_payment_due_date": "2026-10-20"}],
            "mortgage": [{"account_id": "mtg", "interest_rate": {"percentage": 3.1, "type": "fixed"},
                          "next_monthly_payment": 2100, "next_payment_due_date": "2026-10-01"}],
            "credit": [{"account_id": "cc", "aprs": [{"apr_percentage": 24.99, "apr_type": "purchase_apr"}],
                        "minimum_payment_amount": 35, "next_payment_due_date": "2026-10-10"}],
        },
    }
    return f


class Recorder:
    """Wraps a TestClient and keeps every response body for leak checks."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.bodies: list[str] = []

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if name not in ("get", "post", "patch", "delete", "put"):
            return method

        def wrapped(*args, **kwargs):
            r = method(*args, **kwargs)
            self.bodies.append(r.text + str(r.headers))
            return r

        return wrapped


@pytest.fixture
def api(unlocked, plaid_setup) -> Recorder:
    return Recorder(unlocked)


def exchange(api, public_token, kind):
    """Link an Item end to end (Release 2): exchange (pending) + import every account."""
    r = api.post("/api/plaid/exchange", json={"public_token": public_token, "kind": kind})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["item"]["status"] == "pending"
    ids = [a["plaid_account_id"] for a in body["accounts"]]
    r = api.post(f"/api/plaid/items/{body['item']['id']}/import", json={"plaid_account_ids": ids})
    assert r.status_code == 200, r.text
    return r.json()["item"]


def accounts_by_name(api) -> dict[str, dict]:
    return {a["name"]: a for a in api.get("/api/accounts").json()}


# --------------------------------------------------------------------------- tests


@pytest.mark.parametrize(
    ("type_", "subtype", "expected"),
    [
        ("depository", "checking", "bank"),
        ("depository", "savings", "bank"),
        ("depository", "hsa", "hsa"),
        ("investment", "hsa", "hsa"),
        ("investment", "401k", "retirement"),
        ("investment", "roth 401k", "retirement"),
        ("investment", "non-taxable brokerage account", "retirement"),
        ("investment", "brokerage", "investment"),
        ("loan", "student", "loan"),
        ("loan", "mortgage", "loan"),
        ("credit", "credit card", "credit"),
        ("other", None, "other"),
        (None, None, "other"),
    ],
)
def test_category_mapping(type_, subtype, expected):
    assert categorize(type_, subtype) == expected


def test_status_and_link_token(api, fake_plaid):
    # An injected client has no ``source``: configured means it stands in for the .env keys.
    assert api.get("/api/plaid/status").json() == {"configured": True, "env": "sandbox", "source": "env", "items_cap": False, "items_linked": 0, "items_limit": 0, "items_now": 0}
    r = api.post("/api/plaid/link-token", json={"kind": "bank"})
    assert r.status_code == 200 and r.json() == {"link_token": "link-sandbox-bank-new"}
    assert api.post("/api/plaid/link-token", json={"kind": "crypto"}).status_code == 422

    item = exchange(api, "public-bank", "bank")
    r = api.post("/api/plaid/link-token", json={"kind": "bank", "item_id": item["id"]})
    assert r.json() == {"link_token": "link-sandbox-bank-update"}
    assert fake_plaid.calls[-1] == ("link", "bank", BANK_TOKEN)  # update mode used the token
    assert api.post("/api/plaid/link-token", json={"kind": "bank", "item_id": 999}).status_code == 404


def test_bank_exchange_and_sync(api, fake_plaid):
    item = exchange(api, "public-bank", "bank")
    assert item == {"id": item["id"], "institution_name": "First Bank", "kind": "bank", "status": "ok",
                    "error_code": None, "last_synced_at": item["last_synced_at"], "account_count": 2}
    assert item["last_synced_at"].endswith("Z")

    accounts = accounts_by_name(api)
    chk, hsa = accounts["Checking"], accounts["Health Savings"]
    assert chk["category"] == "bank" and chk["source"] == "plaid" and chk["item_id"] == item["id"]
    assert chk["current_balance"] == 1000.5 and chk["available_balance"] == 900.25
    assert chk["institution_name"] == "First Bank" and chk["mask"] == "0000"
    assert chk["plaid_type"] == "depository" and chk["plaid_subtype"] == "checking"
    assert hsa["category"] == "hsa"

    # has_more pagination: both pages consumed, sign flipped.
    txns = api.get("/api/transactions").json()
    assert txns["total"] == 3
    by_id = {t["name"]: t for t in txns["items"]}
    assert by_id["Coffee Shop"]["amount"] == -12.34  # Plaid +12.34 = money out
    assert by_id["Payroll"]["amount"] == 500.0  # Plaid -500 = money in
    assert by_id["Coffee Shop"]["category"] == "FOOD_AND_DRINK"
    assert by_id["Coffee Shop"]["merchant_name"] == "Blue Bottle"
    assert by_id["Pharmacy"]["account_name"] == "Health Savings"
    assert [t["date"] for t in txns["items"]] == ["2026-09-03", "2026-09-01", "2026-09-01"]
    cursors = [c[2] for c in fake_plaid.calls if c[0] == "transactions"]
    assert cursors == [None, "c1"]

    # Second sync: modified + removed from the stored cursor.
    r = api.post("/api/plaid/sync", json={"item_id": item["id"]})
    assert r.status_code == 200
    assert r.json() == {"results": [{
        "item_id": item["id"], "institution_name": "First Bank", "ok": True, "error_code": None,
        "accounts": 2, "transactions_added": 0, "transactions_modified": 1,
        "transactions_removed": 1, "holdings": 0,
    }]}
    txns = api.get("/api/transactions").json()
    assert txns["total"] == 2
    assert {t["name"]: t["amount"] for t in txns["items"]} == {"Coffee Shop": -15.0, "Pharmacy": -40.0}

    summary = api.get("/api/summary").json()
    assert summary["by_category"]["bank"] == 1000.5 and summary["by_category"]["hsa"] == 2500.0
    assert summary["last_synced_at"] is not None


def test_transactions_filters(api):
    exchange(api, "public-bank", "bank")
    chk_id = accounts_by_name(api)["Checking"]["id"]
    assert api.get("/api/transactions", params={"account_id": chk_id}).json()["total"] == 2
    assert api.get("/api/transactions", params={"search": "bLuE"}).json()["total"] == 1
    assert api.get("/api/transactions", params={"search": "%"}).json()["total"] == 0
    assert api.get("/api/transactions", params={"start": "2026-09-02"}).json()["total"] == 1
    assert api.get("/api/transactions", params={"end": "2026-09-01"}).json()["total"] == 2
    page = api.get("/api/transactions", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert len(api.get("/api/transactions", params={"limit": 5000}).json()["items"]) == 3


def test_mutation_during_pagination_restarts(api, fake_plaid):
    fake_plaid.fail(BANK_TOKEN, "transactions", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
    # First call (cursor None) fails, then the loop restarts from the original cursor.
    item = exchange(api, "public-bank", "bank")
    assert item["status"] == "ok"
    assert api.get("/api/transactions").json()["total"] == 3
    cursors = [c[2] for c in fake_plaid.calls if c[0] == "transactions"]
    assert cursors == [None, None, "c1"]


def test_investment_sync_replaces_holdings(api, fake_plaid):
    item = exchange(api, "public-inv", "investment")
    assert item["account_count"] == 3
    accounts = accounts_by_name(api)
    assert accounts["Work 401k"]["category"] == "retirement"
    assert accounts["Brokerage"]["category"] == "investment"
    assert accounts["HSA Invest"]["category"] == "hsa"
    # Balances from the holdings response override /accounts/get.
    assert accounts["Work 401k"]["current_balance"] == 55000.55
    assert not [c for c in fake_plaid.calls if c[0] == "transactions"]

    holdings = api.get("/api/holdings").json()
    assert len(holdings) == 3
    vti = next(h for h in holdings if h["ticker"] == "VTI")
    assert vti == {"id": vti["id"], "account_id": accounts["Work 401k"]["id"], "account_name": "Work 401k",
                   "name": "Vanguard Total Stock Market ETF", "ticker": "VTI", "quantity": 100.5,
                   "price": 250.1, "value": 25135.05, "cost_basis": 20000.0}
    bnd = next(h for h in holdings if h["ticker"] == "BND")
    assert bnd["cost_basis"] is None
    only_brk = api.get("/api/holdings", params={"account_id": accounts["Brokerage"]["id"]}).json()
    assert [h["name"] for h in only_brk] == ["Cash"]

    fake_plaid.holdings[INV_TOKEN]["holdings"] = fake_plaid.holdings[INV_TOKEN]["holdings"][:1]
    r = api.post("/api/plaid/sync", json={})
    assert r.json()["results"][0]["holdings"] == 1
    assert [h["ticker"] for h in api.get("/api/holdings").json()] == ["VTI"]


def test_products_not_supported_is_skipped(api, fake_plaid):
    fake_plaid.fail(INV_TOKEN, "holdings", "NO_INVESTMENT_ACCOUNTS")
    item = exchange(api, "public-inv", "investment")
    assert item["status"] == "ok" and item["error_code"] is None
    assert api.get("/api/holdings").json() == []
    assert accounts_by_name(api)["Work 401k"]["current_balance"] == 50000.0


def test_liabilities_mapping(api):
    exchange(api, "public-loan", "loan")
    accounts = accounts_by_name(api)
    stu, mtg, cc = accounts["Student Loan"], accounts["Mortgage"], accounts["Visa"]
    assert stu["category"] == "loan" and stu["is_liability"] is True
    assert (stu["interest_rate"], stu["minimum_payment"], stu["next_payment_due"]) == (5.25, 280.12, "2026-10-20")
    assert (mtg["interest_rate"], mtg["minimum_payment"], mtg["next_payment_due"]) == (3.1, 2100.0, "2026-10-01")
    assert cc["category"] == "credit"
    assert (cc["interest_rate"], cc["minimum_payment"], cc["next_payment_due"]) == (24.99, 35.0, "2026-10-10")

    summary = api.get("/api/summary").json()
    assert summary["total_liabilities"] == 326234.56
    assert summary["net_worth"] == -326234.56


def test_login_required_and_error_isolation(api, fake_plaid):
    bank = exchange(api, "public-bank", "bank")
    inv = exchange(api, "public-inv", "investment")
    loan = exchange(api, "public-loan", "loan")

    fake_plaid.fail(BANK_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    fake_plaid.fail(INV_TOKEN, "holdings", "INTERNAL_SERVER_ERROR", error_type="API_ERROR")
    fake_plaid.accounts[LOAN_TOKEN][2]["balances"]["current"] = 999.99
    fake_plaid.liabilities[LOAN_TOKEN]["accounts"] = fake_plaid.accounts[LOAN_TOKEN]

    results = {r["item_id"]: r for r in api.post("/api/plaid/sync").json()["results"]}
    assert results[bank["id"]]["ok"] is False
    assert results[bank["id"]]["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert results[inv["id"]]["ok"] is False
    assert results[inv["id"]]["error_code"] == "INTERNAL_SERVER_ERROR"
    assert results[loan["id"]]["ok"] is True  # later item still synced
    assert accounts_by_name(api)["Visa"]["current_balance"] == 999.99

    items = {i["id"]: i for i in api.get("/api/plaid/items").json()}
    assert items[bank["id"]]["status"] == "login_required"
    assert items[bank["id"]]["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert items[inv["id"]]["status"] == "error"
    assert items[inv["id"]]["error_code"] == "INTERNAL_SERVER_ERROR"
    assert items[loan["id"]]["status"] == "ok"
    assert api.get("/api/summary").json()["items_needing_attention"] == 2

    # A failed sync is all-or-nothing for that item: holdings untouched.
    assert len(api.get("/api/holdings").json()) == 3

    # Recovery clears the status.
    r = api.post("/api/plaid/sync", json={"item_id": bank["id"]})
    assert r.json()["results"][0]["ok"] is True
    assert {i["id"]: i["status"] for i in api.get("/api/plaid/items").json()}[bank["id"]] == "ok"


def test_unexpected_exception_is_isolated(api, fake_plaid, monkeypatch):
    bank = exchange(api, "public-bank", "bank")
    loan = exchange(api, "public-loan", "loan")

    original = fake_plaid.get_accounts

    def boom(token):
        if token == BANK_TOKEN:
            raise RuntimeError(f"kaboom {token}")
        return original(token)

    monkeypatch.setattr(fake_plaid, "get_accounts", boom)
    results = {r["item_id"]: r for r in api.post("/api/plaid/sync").json()["results"]}
    assert results[bank["id"]]["error_code"] == "INTERNAL_ERROR"
    assert results[loan["id"]]["ok"] is True


def test_remove_item_cascades(api, fake_plaid):
    bank = exchange(api, "public-bank", "bank")
    manual = api.post("/api/accounts", json={"name": "Cash", "category": "other", "current_balance": 20})
    fake_plaid.fail(BANK_TOKEN, "remove", "INTERNAL_SERVER_ERROR")  # best effort only
    assert api.delete(f"/api/plaid/items/{bank['id']}").status_code == 204
    assert api.get("/api/plaid/items").json() == []
    assert [a["name"] for a in api.get("/api/accounts").json()] == ["Cash"]
    assert api.get("/api/transactions").json() == {"items": [], "total": 0, "next_cursor": None, "days": {}}
    assert manual.status_code == 201
    assert api.delete(f"/api/plaid/items/{bank['id']}").status_code == 404


def test_access_token_never_in_responses(api, fake_plaid, caplog):
    caplog.set_level("DEBUG")
    bank = exchange(api, "public-bank", "bank")
    exchange(api, "public-inv", "investment")
    exchange(api, "public-loan", "loan")
    fake_plaid.fail(INV_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    api.post("/api/plaid/sync")
    api.post("/api/plaid/link-token", json={"kind": "bank", "item_id": bank["id"]})
    for path in ("/api/plaid/items", "/api/accounts", "/api/summary", "/api/transactions",
                 "/api/holdings", "/api/networth/history", "/api/plaid/status"):
        assert api.get(path).status_code == 200
    for account in api.get("/api/accounts").json():
        api.get(f"/api/accounts/{account['id']}/history")
    api.delete(f"/api/plaid/items/{bank['id']}")

    assert len(api.bodies) > 15
    for body in api.bodies:
        for token in ALL_TOKENS:
            assert token not in body
        assert "access_token" not in body
    for token in ALL_TOKENS:
        assert token not in caplog.text


def test_sync_without_items_is_empty(api):
    assert api.post("/api/plaid/sync").json() == {"results": []}
    assert api.post("/api/plaid/sync", json={"item_id": 42}).status_code == 404


# ----------------------------------------------------------- Plaid not configured


@pytest.fixture
def unconfigured(tmp_path):
    app = create_app(make_settings(tmp_path))
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        yield c


def test_link_token_503_when_not_configured(unconfigured):
    assert unconfigured.get("/api/plaid/status").json() == {"configured": False, "env": "sandbox", "source": "none", "items_cap": False, "items_linked": 0, "items_limit": 0, "items_now": 0}
    r = unconfigured.post("/api/plaid/link-token", json={"kind": "bank"})
    assert r.status_code == 503 and isinstance(r.json()["detail"], str)
    r = unconfigured.post("/api/plaid/exchange", json={"public_token": "x", "kind": "bank"})
    assert r.status_code == 503
    assert unconfigured.post("/api/plaid/sync").json() == {"results": []}


def test_link_token_requires_session(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path)), base_url=BASE_URL, headers=CSRF)
    assert c.post("/api/plaid/link-token", json={"kind": "bank"}).status_code == 401


# ------------------------------------------------------------ real client wrapper


def test_api_exception_is_parsed():
    import plaid

    exc = plaid.ApiException(status=400, reason="Bad Request")
    exc.body = '{"error_type":"ITEM_ERROR","error_code":"ITEM_LOGIN_REQUIRED","error_message":"login","request_id":"r"}'
    err = PlaidError.from_api_exception(exc)
    assert (err.error_type, err.error_code, err.error_message) == ("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "login")

    garbage = plaid.ApiException(status=500, reason="oops")
    garbage.body = "<html>"
    err = PlaidError.from_api_exception(garbage)
    assert (err.error_type, err.error_code) == ("API_ERROR", "HTTP_500")


def test_real_client_wraps_api_exception(tmp_path, monkeypatch):
    import plaid

    client = PlaidClient(make_settings(tmp_path, plaid_client_id="cid", plaid_secret="sec"))
    assert client.configured and client.env == "sandbox"

    def raise_api_exception(_request):
        exc = plaid.ApiException(status=400, reason="Bad Request")
        exc.body = '{"error_type":"ITEM_ERROR","error_code":"ITEM_LOGIN_REQUIRED","error_message":"m"}'
        raise exc

    monkeypatch.setattr(client._api, "accounts_get", raise_api_exception)
    with pytest.raises(PlaidError) as info:
        client.get_accounts(BANK_TOKEN)
    assert info.value.error_code == "ITEM_LOGIN_REQUIRED"
    assert BANK_TOKEN not in str(info.value)

    def network_down(_request):
        raise ConnectionError(f"failed sending {BANK_TOKEN}")

    monkeypatch.setattr(client._api, "accounts_get", network_down)
    with pytest.raises(PlaidError) as info:
        client.get_accounts(BANK_TOKEN)
    assert info.value.error_code == "NETWORK_ERROR" and BANK_TOKEN not in info.value.error_message


# ------------------------------------------------- one Item per institution (no duplicates)


def test_link_token_requests_other_products_as_optional(tmp_path, monkeypatch):
    client = PlaidClient(make_settings(tmp_path, plaid_client_id="cid", plaid_secret="sec"))
    seen = {}

    def capture(request):
        seen["products"] = [p.value for p in request.products]
        seen["optional"] = [p.value for p in request.optional_products]

        class Resp:
            def to_dict(self):
                return {"link_token": "link-sandbox-x"}

        return Resp()

    monkeypatch.setattr(client._api, "link_token_create", capture)
    client.create_link_token("loan")
    assert seen == {"products": ["liabilities"], "optional": ["transactions", "investments"]}


def test_sync_follows_item_products_not_link_kind(api, fake_plaid):
    # Linked via "bank", but the Item also got Liabilities as an optional product.
    fake_plaid.products[BANK_TOKEN] = {"transactions", "liabilities"}
    exchange(api, "public-bank", "bank")
    called = {method for method, token, _ in fake_plaid.calls if token == BANK_TOKEN}
    assert {"transactions", "liabilities"} <= called
    assert "holdings" not in called  # never call a product the Item lacks (Plaid would bill it)


def test_duplicate_link_of_same_institution_is_rejected(api, fake_plaid):
    fake_plaid.add_item("public-bank-again", "access-sandbox-bank-2", "item_bank_2", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-bank-2"] = copy.deepcopy(fake_plaid.accounts[BANK_TOKEN])
    exchange(api, "public-bank", "bank")
    before = len(api.get("/api/accounts").json())

    r = api.post("/api/plaid/exchange", json={"public_token": "public-bank-again", "kind": "loan"})
    assert r.status_code == 409 and "already linked" in r.json()["detail"]
    assert "access-sandbox-bank-2" in fake_plaid.removed  # duplicate Item cleaned up at Plaid
    assert len(api.get("/api/accounts").json()) == before
    assert len(api.get("/api/plaid/items").json()) == 1


def test_second_login_at_same_institution_is_allowed(api, fake_plaid):
    fake_plaid.add_item("public-partner", "access-sandbox-partner", "item_partner", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-partner"] = [acct("p_chk", "Partner Checking", "depository", "checking", 50, mask="9999")]
    exchange(api, "public-bank", "bank")
    exchange(api, "public-partner", "bank")
    assert len(api.get("/api/plaid/items").json()) == 2


# ------------------------------------------------- Release 3.10: renamed accounts, connections used


def _no_official_names(fake_plaid, token):
    for pa in fake_plaid.accounts[token]:
        pa["official_name"] = None


def test_duplicate_is_still_found_after_renaming(api, fake_plaid):
    """accounts.name is the user's nickname: the duplicate check keys on the bank's own name
    (account_facts), so renaming every account can't let a second, billed Item in."""
    _no_official_names(fake_plaid, BANK_TOKEN)
    fake_plaid.add_item("public-bank-again", "access-sandbox-bank-2", "item_bank_2", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-bank-2"] = copy.deepcopy(fake_plaid.accounts[BANK_TOKEN])
    exchange(api, "public-bank", "bank")
    for a in api.get("/api/accounts").json():
        assert api.patch(f"/api/accounts/{a['id']}", json={"name": f"My {a['name']}"}).status_code == 200

    r = api.post("/api/plaid/exchange", json={"public_token": "public-bank-again", "kind": "bank"})
    assert r.status_code == 409 and "already linked" in r.json()["detail"]
    assert "access-sandbox-bank-2" in fake_plaid.removed
    assert len(api.get("/api/plaid/items").json()) == 1


def test_duplicate_found_after_renaming_with_official_names(api, fake_plaid):
    fake_plaid.add_item("public-bank-again", "access-sandbox-bank-2", "item_bank_2", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-bank-2"] = copy.deepcopy(fake_plaid.accounts[BANK_TOKEN])
    exchange(api, "public-bank", "bank")
    for a in api.get("/api/accounts").json():
        api.patch(f"/api/accounts/{a['id']}", json={"name": "Renamed"})
    r = api.post("/api/plaid/exchange", json={"public_token": "public-bank-again", "kind": "bank"})
    assert r.status_code == 409


def test_renamed_account_elsewhere_doesnt_block_a_different_login(api, fake_plaid):
    _no_official_names(fake_plaid, BANK_TOKEN)
    fake_plaid.add_item("public-partner", "access-sandbox-partner", "item_partner", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-partner"] = [
        {**acct("p_chk", "Partner Checking", "depository", "checking", 50, mask="9999"), "official_name": None}]
    exchange(api, "public-bank", "bank")
    chk = accounts_by_name(api)["Checking"]
    api.patch(f"/api/accounts/{chk['id']}", json={"name": "Partner Checking"})  # nickname collides by chance
    exchange(api, "public-partner", "bank")
    assert len(api.get("/api/plaid/items").json()) == 2


def _linked(api) -> int:
    body = api.get("/api/plaid/status").json()
    assert body["items_limit"] == (10 if body["items_cap"] else 0)
    return body["items_linked"]


def test_connections_used_counter(api, fake_plaid):
    assert _linked(api) == 0
    exchange(api, "public-bank", "bank")
    assert _linked(api) == 1
    # Exchanging the same Item again (e.g. the pick step reopened) is not a new connection.
    r = api.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"})
    assert r.status_code == 201 and _linked(api) == 1
    # A rejected duplicate was still created at Plaid: it used a connection.
    fake_plaid.add_item("public-bank-again", "access-sandbox-bank-2", "item_bank_2", "First Bank", inst_id="ins_item_bank")
    fake_plaid.accounts["access-sandbox-bank-2"] = copy.deepcopy(fake_plaid.accounts[BANK_TOKEN])
    assert api.post("/api/plaid/exchange", json={"public_token": "public-bank-again", "kind": "bank"}).status_code == 409
    assert _linked(api) == 2
    # Removing a connection doesn't give it back.
    item_id = api.get("/api/plaid/items").json()[0]["id"]
    assert api.delete(f"/api/plaid/items/{item_id}").status_code == 204
    assert _linked(api) == 2
    exchange(api, "public-inv", "investment")
    assert _linked(api) == 3


def test_connections_used_counter_seeds_from_existing_items(api, vault):
    from app.models import AppSetting, PlaidItem

    s = vault.db.acquire()
    try:
        s.add_all([PlaidItem(plaid_item_id=f"old{i}", access_token=f"t{i}", kind="bank", status="ok") for i in range(3)])
        s.commit()
        assert _linked(api) == 3  # never stored: the Items there are now
        assert s.get(AppSetting, "plaid_items_linked") is None  # a GET doesn't write
        exchange(api, "public-bank", "bank")
        assert _linked(api) == 4
        s.expire_all()
        assert s.get(AppSetting, "plaid_items_linked").value == "4"
        s.get(AppSetting, "plaid_items_linked").value = "junk"
        s.commit()
        assert _linked(api) == 4  # unreadable: at least the Items there are
    finally:
        vault.db.release(s)


def test_owner_corrects_connections_used(api, fake_plaid):
    """Settings › Banks "Change…": the real count from Plaid's dashboard (connections removed
    before Release 3.10 weren't counted), from the connections there are now up to 10."""
    exchange(api, "public-bank", "bank")
    assert api.put("/api/plaid/items-cap", json={"on": True}).status_code == 200
    r = api.put("/api/plaid/items-linked", json={"count": 7})
    assert r.status_code == 200, r.text
    assert r.json() == {"items_cap": True, "items_linked": 7, "items_limit": 10, "items_now": 1}
    assert _linked(api) == 7
    assert api.get("/api/plaid/status").json()["items_now"] == 1
    # The next new connection counts from there.
    exchange(api, "public-inv", "investment")
    assert _linked(api) == 8
    # Down to the connections there are now is allowed (an over-count can be fixed).
    assert api.put("/api/plaid/items-linked", json={"count": 2}).json()["items_linked"] == 2
    r = api.put("/api/plaid/items-linked", json={"count": 1})  # fewer than there are now
    assert r.status_code == 422 and "from 2 to 10" in r.json()["detail"]
    for bad in (11, -1, 3.5, 5.0, "5", True, None, 10**12):
        r = api.put("/api/plaid/items-linked", json={"count": bad})
        assert r.status_code == 422, bad
    assert api.put("/api/plaid/items-linked", json={"count": 3, "extra": 1}).status_code == 422
    assert api.put("/api/plaid/items-linked", json={}).status_code == 422
    assert _linked(api) == 2
    assert api.put("/api/plaid/items-linked", json={"count": 10}).json()["items_linked"] == 10


def test_connections_used_route_needs_the_session_and_header(api):
    from app.security import SESSION_HEADER

    assert api.put("/api/plaid/items-linked", json={"count": 5}, headers={"X-FinTrack": "0"}).status_code == 403
    assert api.put("/api/plaid/items-linked", json={"count": 5}, headers={SESSION_HEADER: "wrong"}).status_code == 401
    assert _linked(api) == 0
