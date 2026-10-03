"""Release 3 request hardening: unknown fields, NaN/Infinity and size bounds are all 422."""
from __future__ import annotations

import json

import pytest

from tests.conftest import add_txn, make_account


@pytest.fixture
def ids(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    loan = make_account(db, "Loan", "loan", 100, minimum_payment_cents=1000)
    t = add_txn(db, chk.id, "2026-09-10", -10, "Coffee")
    group = unlocked.post("/api/category-groups", json={"name": "G"}).json()
    tag = unlocked.post("/api/tags", json={"name": "T"}).json()
    return {"txn": t.id, "chk": chk.id, "loan": loan.id, "group": group["id"], "tag": tag["id"]}


def bodies(ids) -> list[tuple[str, str, dict]]:
    """(method, url, a valid body) for every Release 3 JSON body."""
    return [
        ("POST", "/api/category-groups/reorder", {"ids": [ids["group"]]}),  # before another group exists
        ("POST", "/api/category-groups", {"name": "New"}),
        ("PATCH", f"/api/category-groups/{ids['group']}", {"name": "Renamed"}),
        ("PATCH", "/api/categories/TRAVEL", {"group_id": None, "target": {"kind": "monthly", "amount": 5}}),
        ("PUT", f"/api/transactions/{ids['txn']}/splits",
         {"splits": [{"amount": -4, "category": "OTHER"}, {"amount": -6, "category": "TRAVEL"}]}),
        ("POST", "/api/transactions/recategorize/preview", {"field": "name", "text": "Coffee", "category": "OTHER"}),
        ("POST", "/api/transactions/recategorize",
         {"field": "name", "text": "Coffee", "category": "OTHER", "include_manual": False}),
        ("POST", "/api/tags", {"name": "Other tag"}),
        ("PATCH", f"/api/tags/{ids['tag']}", {"hue": 5}),
        ("PUT", f"/api/transactions/{ids['txn']}/tags", {"tag_ids": [ids["tag"]]}),
        ("POST", "/api/debt/plan", {"extra": 0, "strategy": "avalanche"}),
        ("PUT", "/api/backup/auto", {"dir": None, "keep": 3}),
        ("PUT", "/api/plaid/auto-sync", {"hours": 6}),
        ("PATCH", f"/api/accounts/{ids['loan']}", {"loan_group": "Mine"}),
        # Release 3.3
        ("PATCH", "/api/transactions", {"ids": [ids["txn"]], "category": "TRAVEL"}),
        ("POST", "/api/transactions/restore",
         {"items": [{"id": ids["txn"], "category": None, "category_source": "plaid"}]}),
    ]


def test_valid_bodies_pass_and_unknown_fields_are_refused(unlocked, ids):
    for method, url, body in bodies(ids):
        r = unlocked.request(method, url, json={**body, "sneaky": 1})
        assert r.status_code == 422, (method, url, r.status_code)
    for method, url, body in bodies(ids):
        r = unlocked.request(method, url, json=body)
        assert r.status_code in (200, 201), (method, url, r.text)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_refused(unlocked, ids, literal):
    cases = [
        ("PATCH", "/api/categories/TRAVEL", '{"target": {"kind": "monthly", "amount": %s}}'),
        ("PUT", f"/api/transactions/{ids['txn']}/splits",
         '{"splits": [{"amount": %s, "category": "OTHER"}, {"amount": -10, "category": "OTHER"}]}'),
        ("POST", "/api/debt/plan", '{"extra": %s, "strategy": "snowball"}'),
    ]
    for method, url, template in cases:
        r = unlocked.request(method, url, content=template % literal, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (url, literal)


def test_size_bounds(unlocked, ids):
    c = unlocked
    txn = ids["txn"]
    too_big = [
        ("POST", "/api/category-groups", {"name": "g" * 61}),
        ("POST", "/api/tags", {"name": "t" * 41}),
        ("PUT", f"/api/transactions/{txn}/splits", {"splits": [{"amount": -0.5, "category": "OTHER"}] * 21}),
        ("PUT", f"/api/transactions/{txn}/splits",
         {"splits": [{"amount": -5, "category": "OTHER", "notes": "n" * 501}, {"amount": -5, "category": "OTHER"}]}),
        ("PUT", f"/api/transactions/{txn}/tags", {"tag_ids": list(range(1, 22))}),
        ("POST", "/api/transactions/recategorize/preview", {"field": "name", "text": "x" * 201, "category": "OTHER"}),
        ("POST", "/api/debt/plan", {"extra": 0, "strategy": "custom", "order": list(range(1, 102))}),
        ("POST", "/api/debt/plan", {"extra": 1e13, "strategy": "snowball"}),
        ("POST", "/api/category-groups/reorder", {"ids": list(range(1, 1002))}),
        ("PATCH", f"/api/accounts/{ids['loan']}", {"loan_group": "l" * 41}),
        ("PUT", "/api/backup/auto", {"dir": "d" * 1001, "keep": 3}),
        ("PATCH", "/api/transactions", {"ids": list(range(1, 1002)), "category": None}),
        ("PATCH", "/api/transactions", {"ids": [txn], "category": "c" * 65}),
        ("POST", "/api/transactions/restore",
         {"items": [{"id": i, "category": None, "category_source": "plaid"} for i in range(1, 1002)]}),
        ("POST", "/api/transactions/restore",
         {"items": [{"id": txn, "category": "c" * 65, "category_source": "user"}]}),
    ]
    for method, url, body in too_big:
        assert c.request(method, url, json=body).status_code == 422, url
    assert c.get("/api/reports/monthly", params={"months": 61}).status_code == 422
    assert c.get(f"/api/loans/{ids['loan']}/payoff", params={"extra": 2e12}).status_code == 422
    assert c.get("/api/transactions", params={"tag": 2**63}).status_code == 422
    for url in ("/api/transactions", "/api/transactions/summary", "/api/transactions/ids",
                "/api/transactions/export.csv"):
        assert c.get(url, params={"view": "bogus"}).status_code == 422, url
        assert c.get(url, params={"search": "s" * 201}).status_code == 422, url
        assert c.get(url, params={"category": "c" * 65}).status_code == 422, url
        assert c.get(url, params={"account_id": 2**63}).status_code == 422, url
    for cursor in ("2026-09-01.1' OR 1=1 --", "2026-09-01.99999999999999999999", "2026-09-01." + "1" * 29, "2026-09-01.١٢"):
        assert c.get("/api/transactions", params={"cursor": cursor}).status_code == 422, cursor


def test_group_and_tag_counts_are_bounded(unlocked, monkeypatch):
    from app.routers import category_groups
    from app.services import txns

    monkeypatch.setattr(category_groups, "MAX_GROUPS", 2)
    monkeypatch.setattr(txns, "MAX_TAGS", 1)
    assert unlocked.post("/api/category-groups", json={"name": "A"}).status_code == 201
    assert unlocked.post("/api/category-groups", json={"name": "B"}).status_code == 201
    assert unlocked.post("/api/category-groups", json={"name": "C"}).status_code == 422
    assert unlocked.post("/api/category-groups/starter", json={}).status_code == 422
    assert unlocked.post("/api/tags", json={"name": "A"}).status_code == 201
    assert unlocked.post("/api/tags", json={"name": "B"}).status_code == 422


def test_release3_routes_need_a_session(client):
    for method, url in (("GET", "/api/category-groups"), ("GET", "/api/tags"), ("GET", "/api/reports/monthly"),
                        ("GET", "/api/reports/spending"), ("GET", "/api/loans/1/payoff"), ("POST", "/api/debt/plan"),
                        ("GET", "/api/backup/auto"), ("PUT", "/api/backup/auto"), ("GET", "/api/plaid/auto-sync"),
                        ("PUT", "/api/plaid/auto-sync"), ("POST", "/api/budgets/2026-09/fund-targets"),
                        ("PUT", "/api/transactions/1/splits"), ("PUT", "/api/transactions/1/tags"),
                        ("POST", "/api/transactions/recategorize"), ("GET", "/api/transactions/summary"),
                        ("GET", "/api/transactions/ids"), ("PATCH", "/api/transactions"),
                        ("POST", "/api/transactions/restore"), ("GET", "/api/transactions/1/related")):
        r = client.request(method, url, json={} if method != "GET" else None)
        assert r.status_code == 401, (method, url)


def test_forecast_daily_spend_is_split_aware(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 3000, plaid_type="depository", plaid_subtype="checking")
    t = add_txn(db, chk.id, "2026-09-20", -90, "Costco", category="FOOD_AND_DRINK")
    assert unlocked.get("/api/forecast").json()["daily_spend"] == 12.86  # 90 over 7 days
    r = unlocked.put(f"/api/transactions/{t.id}/splits", json={"splits": [
        {"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -30, "category": "LOAN_PAYMENTS"}]})
    assert r.status_code == 200
    assert unlocked.get("/api/forecast").json()["daily_spend"] == 8.57  # only the 60 of spending
    assert json.dumps(r.json())  # serializable
