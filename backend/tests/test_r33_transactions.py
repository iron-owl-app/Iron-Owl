"""Release 3.3 Transactions redesign: needs a category, views, cursor paging, day totals,
summary, suggestions, the matching rule, set several at once, Undo (restore) and related."""
from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import select

from tests.conftest import add_txn, make_account


def ok(response) -> dict:
    assert response.status_code in (200, 201), response.text
    return response.json()


def listed(client, **params) -> list[dict]:
    return ok(client.get("/api/transactions", params={"limit": 500, **params}))["items"]


def ids_of(client, **params) -> list[int]:
    return [t["id"] for t in listed(client, **params)]


def by_id(client, **params) -> dict[int, dict]:
    return {t["id"]: t for t in listed(client, **params)}


def split(client, txn_id, a, b, category_a="FOOD_AND_DRINK", category_b="TRAVEL"):
    ok(client.put(f"/api/transactions/{txn_id}/splits", json={"splits": [
        {"amount": a, "category": category_a}, {"amount": b, "category": category_b}]}))


def new_group(client, name="Everyday", categories=()) -> int:
    gid = ok(client.post("/api/category-groups", json={"name": name}))["id"]
    for category in categories:
        ok(client.patch(f"/api/categories/{category}", json={"group_id": gid}))
    return gid


def state(t: dict) -> tuple:
    return t["category"], t["category_source"], t["rule_id"]


# ------------------------------------------------------------------ 1. needs a category


@pytest.fixture
def needs(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    hidden = ok(c.post("/api/categories", json={"name": "Old stuff", "kind": "spending"}))["id"]
    ok(c.patch(f"/api/categories/{hidden}", json={"hidden": True}))
    rows = {
        "null": add_txn(db, acct.id, "2026-09-01", -1, "Null cat", category=None),
        "other": add_txn(db, acct.id, "2026-09-02", -2, "Other cat", category="OTHER"),
        "unknown": add_txn(db, acct.id, "2026-09-03", -3, "Unknown cat", category="NOT_A_REAL_ONE"),
        "hidden": add_txn(db, acct.id, "2026-09-04", -4, "Hidden cat", category=hidden),
        "ungrouped": add_txn(db, acct.id, "2026-09-05", -5, "Ungrouped", category="GENERAL_MERCHANDISE"),
        "grouped": add_txn(db, acct.id, "2026-09-06", -6, "Grouped", category="TRAVEL"),
        "transfer_kind": add_txn(db, acct.id, "2026-09-07", -7, "Transfer kind", category="TRANSFER_OUT"),
        "user": add_txn(db, acct.id, "2026-09-08", -8, "User", category="OTHER", category_source="user"),
        "user_bulk": add_txn(db, acct.id, "2026-09-08", -8, "Bulk", category=None, category_source="user_bulk"),
        "rule": add_txn(db, acct.id, "2026-09-09", -9, "Rule", category="GENERAL_MERCHANDISE",
                        category_source="rule"),
        "is_transfer": add_txn(db, acct.id, "2026-09-10", -10, "Flagged", category=None, is_transfer=True),
        "split": add_txn(db, acct.id, "2026-09-11", -11, "Split", category=None),
        "income_ungrouped": add_txn(db, acct.id, "2026-09-12", 12, "Pay", category="INCOME"),
    }
    split(c, rows["split"].id, -5, -6)
    return c, {k: v.id for k, v in rows.items()}


def _needs_view(client) -> set[int]:
    return set(ids_of(client, view="needs_category"))


def _assert_flag_matches_view(client) -> None:
    flagged = {t["id"] for t in listed(client) if t["needs_category"]}
    assert flagged == _needs_view(client)


def _assert_sql_matches_python(db) -> None:
    from app.models import Transaction, TransactionSplit
    from app.services import txns
    from app.services.categories import load

    db.expire_all()
    cmap = load(db)
    grouped = txns.has_groups(db)
    split_ids = set(db.scalars(select(TransactionSplit.transaction_id)))
    python = {t.id for t in db.scalars(select(Transaction)) if txns.needs_category(t, cmap, grouped, t.id in split_ids)}
    sql = set(db.scalars(select(Transaction.id).where(txns.needs_category_clause())))
    assert python == sql


def test_needs_category_without_groups(needs, db):
    c, ids = needs
    assert _needs_view(c) == {ids[k] for k in ("null", "other", "unknown", "hidden")}
    _assert_flag_matches_view(c)
    _assert_sql_matches_python(db)


def test_needs_category_with_groups_adds_ungrouped_bank_categories(needs, db):
    c, ids = needs
    new_group(c, "Spending", ["TRAVEL"])
    assert _needs_view(c) == {ids[k] for k in ("null", "other", "unknown", "hidden", "ungrouped",
                                                "income_ungrouped")}
    _assert_flag_matches_view(c)
    _assert_sql_matches_python(db)
    # A hidden category stays "needs" even inside a group; Other too.
    hidden = [x["id"] for x in ok(c.get("/api/categories")) if x["hidden"]]
    new_group(c, "More", ["GENERAL_MERCHANDISE", "INCOME", "OTHER", *hidden])
    assert _needs_view(c) == {ids[k] for k in ("null", "other", "unknown", "hidden")}
    _assert_flag_matches_view(c)
    _assert_sql_matches_python(db)


def test_needs_category_row_flag_and_setting_it(needs):
    c, ids = needs
    rows = by_id(c)
    assert rows[ids["split"]]["needs_category"] is False and rows[ids["split"]]["suggested_categories"] == []
    bulk_row = rows[ids["user_bulk"]]
    assert bulk_row["category_source"] == "user_bulk" and bulk_row["needs_category"] is False
    t = ok(c.patch(f"/api/transactions/{ids['null']}", json={"category": "TRAVEL"}))
    assert t["needs_category"] is False and t["category_source"] == "user"
    assert ids["null"] not in _needs_view(c)


# ------------------------------------------------------------------ 2. views by sign, CSV


def test_in_and_out_views_and_csv(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    t_in = add_txn(db, acct.id, "2026-09-01", 10, "Refund", category="INCOME")
    t_out = add_txn(db, acct.id, "2026-09-02", -5, "Coffee Shop")
    t_zero = add_txn(db, acct.id, "2026-09-03", 0, "Zero")
    assert ids_of(c, view="in") == [t_in.id]
    assert ids_of(c, view="out") == [t_out.id]
    assert set(ids_of(c, view="all")) == {t_in.id, t_out.id, t_zero.id}
    assert set(ids_of(c)) == {t_in.id, t_out.id, t_zero.id}
    assert c.get("/api/transactions", params={"view": "bogus"}).status_code == 422

    def csv_rows(**params):
        r = c.get("/api/transactions/export.csv", params=params)
        assert r.status_code == 200
        return list(csv.reader(io.StringIO(r.content.decode("utf-8"))))[1:]

    assert [row[2] for row in csv_rows(view="out")] == ["Coffee Shop"]
    assert [row[2] for row in csv_rows(view="in")] == ["Refund"]
    assert len(csv_rows()) == 3
    assert c.get("/api/transactions/export.csv", params={"view": "bogus"}).status_code == 422


# ------------------------------------------------------------------ 3. cursor paging


@pytest.fixture
def many(unlocked, db, fixed_today):
    acct = make_account(db, "Checking", "bank", 100)
    rows = []
    for i in range(23):
        day = f"2026-09-{1 + i // 3:02d}"  # three per day: ties on date are broken by id
        rows.append(add_txn(db, acct.id, day, -(i + 1), f"Shop {i}", category=None if i % 2 else "TRAVEL"))
    return unlocked, acct, rows


def _walk(client, limit, **params) -> list[int]:
    out, cursor, pages = [], None, 0
    while True:
        q = {"limit": limit, **params}
        if cursor:
            q["cursor"] = cursor
        page = ok(client.get("/api/transactions", params=q))
        out += [t["id"] for t in page["items"]]
        cursor = page["next_cursor"]
        pages += 1
        assert pages < 100
        if cursor is None:
            return out


def test_cursor_paging_concatenates_to_the_full_list(many):
    c, _acct, _rows = many
    full = ids_of(c)
    assert len(full) == 23
    for limit in (1, 3, 5, 7, 23, 50):
        walked = _walk(c, limit)
        assert walked == full and len(set(walked)) == len(walked)
    first = ok(c.get("/api/transactions", params={"limit": 5}))
    assert first["total"] == 23 and len(first["items"]) == 5
    last = first["items"][-1]
    assert first["next_cursor"] == f"{last['date']}.{last['id']}"
    assert ok(c.get("/api/transactions", params={"limit": 23}))["next_cursor"] is None
    # offset still works for old callers (cursor with offset=0 is fine too)
    assert [t["id"] for t in listed(c, offset=5)] == full[5:]
    assert ok(c.get("/api/transactions", params={"cursor": first["next_cursor"], "offset": 0, "limit": 500}))[
        "items"][0]["id"] == full[5]


def test_needs_view_page_two_after_categorizing_page_one(many):
    c, _acct, _rows = many
    full = ids_of(c, view="needs_category")
    assert len(full) == 11
    page1 = ok(c.get("/api/transactions", params={"view": "needs_category", "limit": 4}))
    assert [t["id"] for t in page1["items"]] == full[:4]
    for t in page1["items"]:
        ok(c.patch(f"/api/transactions/{t['id']}", json={"category": "TRAVEL"}))
    page2 = ok(c.get("/api/transactions", params={
        "view": "needs_category", "limit": 4, "cursor": page1["next_cursor"]}))
    assert [t["id"] for t in page2["items"]] == full[4:8]
    assert page2["total"] == 7


@pytest.mark.parametrize("cursor", [
    "abc", "2026-09-01", "2026-09-01.", "2026-13-01.5", "2026-02-30.5", "2026-09-01.-5", "2026-9-01.5",
    "2026-09-01.99999999999999999999", "2026-09-01.9999999999999999999", "2026-09-01.5 OR 1=1",
    "x" * 41,
])
def test_bad_cursor_is_refused(many, cursor):
    c, _acct, _rows = many
    assert c.get("/api/transactions", params={"cursor": cursor}).status_code == 422


def test_cursor_with_offset_is_refused(many):
    c, _acct, _rows = many
    assert c.get("/api/transactions", params={"cursor": "2026-09-05.3", "offset": 2}).status_code == 422


# ------------------------------------------------------------------ 4. day totals


def test_day_totals_cover_the_whole_day_across_pages(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    other = make_account(db, "Savings", "bank", 100)
    add_txn(db, acct.id, "2026-09-26", 100, "Pay", category="INCOME")
    add_txn(db, acct.id, "2026-09-26", -20, "Coffee Shop")
    add_txn(db, acct.id, "2026-09-26", -500, "To savings", category="TRANSFER_OUT", is_transfer=True)
    add_txn(db, acct.id, "2026-09-26", -30.5, "Groceries")
    add_txn(db, other.id, "2026-09-26", -1000, "Other account")
    add_txn(db, acct.id, "2026-09-25", -7, "Earlier")
    page1 = ok(c.get("/api/transactions", params={"limit": 2, "account_id": acct.id}))
    assert list(page1["days"]) == ["2026-09-26"]
    assert page1["days"]["2026-09-26"] == {"money_in": 100.0, "money_out": -50.5}
    page2 = ok(c.get("/api/transactions", params={"limit": 3, "account_id": acct.id,
                                                  "cursor": page1["next_cursor"]}))
    assert page2["days"] == {"2026-09-26": {"money_in": 100.0, "money_out": -50.5},
                             "2026-09-25": {"money_in": 0.0, "money_out": -7.0}}
    # The view is part of the filter set.
    out = ok(c.get("/api/transactions", params={"limit": 1, "account_id": acct.id, "view": "out"}))
    assert out["days"]["2026-09-26"] == {"money_in": 0.0, "money_out": -50.5}


# ------------------------------------------------------------------ 5. summary


def test_summary(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    other = make_account(db, "Card", "credit", 0)
    add_txn(db, acct.id, "2026-08-15", 2000, "Pay", category="INCOME")
    add_txn(db, acct.id, "2026-09-02", -40, "Coffee Shop", category=None)
    add_txn(db, acct.id, "2026-09-03", -60, "Book Store", category="OTHER")
    add_txn(db, acct.id, "2026-09-04", 25, "Refund", category=None)
    add_txn(db, acct.id, "2026-09-05", -300, "To card", category="TRANSFER_OUT", is_transfer=True)
    add_txn(db, acct.id, "2026-09-06", 0, "Zero", category="TRAVEL")
    card = add_txn(db, other.id, "2026-08-01", -12, "Coffee Shop", category="TRAVEL")
    tag = ok(c.post("/api/tags", json={"name": "Trip"}))["id"]
    ok(c.put(f"/api/transactions/{card.id}/tags", json={"tag_ids": [tag]}))

    s = ok(c.get("/api/transactions/summary"))
    # Money excludes the transfer (-300); the lists still show it.
    assert s == {"total": 7, "first_date": "2026-08-01", "money_in": 2025.0, "money_out": -112.0,
                 "needs_category": 3, "counts": {"all": 7, "needs_category": 3, "in": 2, "out": 4}}
    s = ok(c.get("/api/transactions/summary", params={"view": "out"}))
    assert s["total"] == 4 and s["money_in"] == 0.0 and s["money_out"] == -112.0
    assert s["needs_category"] == 2  # needs ∩ out
    assert s["counts"] == {"all": 7, "needs_category": 3, "in": 2, "out": 4}  # counts ignore the view
    s = ok(c.get("/api/transactions/summary", params={"view": "needs_category"}))
    assert s["total"] == 3 and s["first_date"] == "2026-09-02" and s["money_in"] == 25.0

    s = ok(c.get("/api/transactions/summary", params={"account_id": other.id}))
    assert s["total"] == 1 and s["counts"]["all"] == 1 and s["money_out"] == -12.0
    s = ok(c.get("/api/transactions/summary", params={"search": "coffee"}))
    assert s["total"] == 2 and s["needs_category"] == 1 and s["first_date"] == "2026-08-01"
    s = ok(c.get("/api/transactions/summary", params={"tag": tag}))
    assert s["total"] == 1 and s["money_out"] == -12.0
    s = ok(c.get("/api/transactions/summary", params={"start": "2026-10-01"}))
    assert s == {"total": 0, "first_date": None, "money_in": 0.0, "money_out": 0.0, "needs_category": 0,
                 "counts": {"all": 0, "needs_category": 0, "in": 0, "out": 0}}
    assert c.get("/api/transactions/summary", params={"view": "nope"}).status_code == 422


def test_ids_endpoint(many, monkeypatch):
    c, _acct, _rows = many
    full = ids_of(c)
    r = ok(c.get("/api/transactions/ids"))
    assert r == {"ids": full, "total": 23, "truncated": False}
    needing = ok(c.get("/api/transactions/ids", params={"needs_category": 1}))
    assert needing["ids"] == ids_of(c, view="needs_category") and needing["total"] == 11
    from app.routers import transactions

    monkeypatch.setattr(transactions, "MAX_IDS", 5)
    r = ok(c.get("/api/transactions/ids"))
    assert r == {"ids": full[:5], "total": 23, "truncated": True}


# ------------------------------------------------------------------ 6. suggestions


@pytest.fixture
def history(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    hidden = ok(c.post("/api/categories", json={"name": "Retired", "kind": "spending"}))["id"]
    new_group(c, "Everyday", ["FOOD_AND_DRINK", "TRAVEL", "ENTERTAINMENT", "MEDICAL", "PERSONAL_CARE",
                              "OTHER", hidden])
    ok(c.patch(f"/api/categories/{hidden}", json={"hidden": True}))

    def past(day, category, merchant="Coffee Shop", name="COFFEE SHOP 123", **kw):
        return add_txn(db, acct.id, day, -3, name, merchant=merchant, category=category, **kw)

    for day in ("2026-08-01", "2026-08-02", "2026-09-01"):
        past(day, "FOOD_AND_DRINK", category_source="user")
    past("2026-08-03", "TRAVEL", merchant="coffee shop")  # case differs: same merchant
    past("2026-09-10", "TRAVEL", category_source="rule")
    past("2026-08-04", "ENTERTAINMENT")
    past("2026-09-05", "ENTERTAINMENT")
    for i in range(5):
        past(f"2026-07-{i + 1:02d}", "OTHER", category_source="user")
        past(f"2026-07-{i + 1:02d}", hidden, category_source="user")
        past(f"2026-07-{i + 1:02d}", "GENERAL_MERCHANDISE", category_source="user")  # ungrouped
        past(f"2026-07-{i + 1:02d}", "MEDICAL", is_transfer=True)
        s = past(f"2026-07-{i + 11:02d}", "MEDICAL")
        split(c, s.id, -1, -2, "MEDICAL", "MEDICAL")
    for day in ("2026-08-05", "2026-08-06"):
        add_txn(db, acct.id, day, -9, "Corner Deli 5678", category="PERSONAL_CARE")
    target = past("2026-09-20", None)
    by_name = add_txn(db, acct.id, "2026-09-21", -9, "CORNER DELI 1234", category="OTHER")
    settled = past("2026-09-22", "TRAVEL", merchant="Tea House", name="TEA HOUSE", category_source="user")
    unknown = add_txn(db, acct.id, "2026-09-23", -1, "Brand New Place", category=None)
    return c, {"target": target.id, "by_name": by_name.id, "settled": settled.id, "unknown": unknown.id}


def test_suggestions(history):
    c, ids = history
    rows = by_id(c)
    assert rows[ids["target"]]["needs_category"] is True
    # FOOD 3 wins on count; TRAVEL and ENTERTAINMENT tie on 2, TRAVEL used more recently.
    assert rows[ids["target"]]["suggested_categories"] == ["FOOD_AND_DRINK", "TRAVEL"]
    assert rows[ids["by_name"]]["suggested_categories"] == ["PERSONAL_CARE"]  # name-keyed fallback
    assert rows[ids["settled"]]["suggested_categories"] == []  # not needing a category
    assert rows[ids["unknown"]]["suggested_categories"] == []
    # Same result through every response shape (PATCH, filtered list).
    t = ok(c.patch(f"/api/transactions/{ids['target']}", json={"notes": "hi"}))
    assert t["suggested_categories"] == ["FOOD_AND_DRINK", "TRAVEL"]
    page = ok(c.get("/api/transactions", params={"view": "needs_category", "limit": 1}))
    assert page["items"][0]["suggested_categories"] == []  # Brand New Place, newest


def test_suggestion_never_leaves_the_row_needing_a_category(history, db):
    c, ids = history
    for t in listed(c):
        for category in t["suggested_categories"]:
            r = ok(c.patch(f"/api/transactions/{t['id']}", json={"category": category}))
            assert r["needs_category"] is False
            ok(c.patch(f"/api/transactions/{t['id']}", json={"category": None}))
    # Ungrouping FOOD makes it a "needs" category, so it's no longer suggested.
    ok(c.patch("/api/categories/FOOD_AND_DRINK", json={"group_id": None}))
    assert by_id(c)[ids["target"]]["suggested_categories"] == ["TRAVEL", "ENTERTAINMENT"]


def test_suggestions_are_batched(history, db, vault):
    """A page costs a fixed number of queries, not one per row needing a category."""
    from sqlalchemy import event

    c, _ids = history
    acct = make_account(db, "Card", "credit", 0)
    for i in range(40):
        add_txn(db, acct.id, "2026-06-01", -1, f"SHOP {i}", merchant=f"Shop {i}", category=None)
        add_txn(db, acct.id, "2026-05-01", -1, f"SHOP {i}", merchant=f"Shop {i}", category="TRAVEL")

    def count(limit):
        statements = []
        engine = vault.db._engine

        def before(*_args):
            statements.append(1)

        event.listen(engine, "before_cursor_execute", before)
        try:
            ok(c.get("/api/transactions", params={"limit": limit}))
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return len(statements)

    assert count(5) == count(200)
    assert all(t["suggested_categories"] == ["TRAVEL"] for t in listed(c, account_id=acct.id) if t["needs_category"])


def test_suggestions_match_merchants_that_differ_in_spacing_or_accented_case(history, db):
    c, _ids = history
    acct = make_account(db, "Card", "credit", 0)
    for _ in range(2):
        add_txn(db, acct.id, "2026-05-01", -1, "BLUE BOTTLE", merchant="Blue  Bottle", category="TRAVEL")
        add_txn(db, acct.id, "2026-05-01", -1, "CAFE ROUGE", merchant="CAFÉ ROUGE", category="TRAVEL")
    spaced = add_txn(db, acct.id, "2026-06-01", -1, "BLUE BOTTLE", merchant="Blue Bottle", category=None)
    accented = add_txn(db, acct.id, "2026-06-01", -1, "CAFE ROUGE", merchant="Café Rouge", category=None)
    rows = by_id(c)
    assert rows[spaced.id]["suggested_categories"] == ["TRAVEL"]
    assert rows[accented.id]["suggested_categories"] == ["TRAVEL"]


# ------------------------------------------------------------------ 7. matching rule


def test_matching_rule_id(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    coffee = add_txn(db, acct.id, "2026-09-01", -4, "COFFEE SHOP 1", merchant="Coffee Shop")
    other = add_txn(db, acct.id, "2026-09-02", -4, "Book Store")
    parted = add_txn(db, acct.id, "2026-09-03", -4, "COFFEE SHOP 2", merchant="Coffee Shop")
    split(c, parted.id, -1, -3)
    off = ok(c.post("/api/rules", json={"field": "merchant", "op": "is", "text": "coffee shop",
                                        "action": "category", "category": "OTHER", "enabled": False}))
    first = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "coffee",
                                          "action": "category", "category": "TRAVEL"}))
    second = ok(c.post("/api/rules", json={"field": "name", "op": "contains", "text": "shop",
                                           "action": "transfer"}))
    rows = by_id(c)
    assert rows[coffee.id]["matching_rule_id"] == first["id"]
    assert rows[other.id]["matching_rule_id"] is None
    assert rows[parted.id]["matching_rule_id"] is None  # rules never touch split rows
    ok(c.post("/api/rules/reorder", json={"ids": [second["id"], first["id"], off["id"]]}))
    assert by_id(c)[coffee.id]["matching_rule_id"] == second["id"]
    # A hand-set row still shows the rule that matches it.
    t = ok(c.patch(f"/api/transactions/{coffee.id}", json={"category": "MEDICAL"}))
    assert t["matching_rule_id"] == second["id"] and t["rule_id"] is None


# ------------------------------------------------------------------ 8. set several at once


@pytest.fixture
def bulk(unlocked, db, fixed_today):
    acct = make_account(db, "Checking", "bank", 100)
    rows = {
        "a": add_txn(db, acct.id, "2026-09-01", -1, "Coffee Shop", merchant="Coffee Shop", category="FOOD_AND_DRINK"),
        "b": add_txn(db, acct.id, "2026-09-02", -2, "Book Store", category=None),
        "c": add_txn(db, acct.id, "2026-09-03", -3, "Gas Station", category="OTHER", category_source="user"),
        "s": add_txn(db, acct.id, "2026-09-04", -4, "Split one", category="TRAVEL"),
    }
    split(unlocked, rows["s"].id, -1, -3)
    return unlocked, {k: v.id for k, v in rows.items()}


def test_bulk_set_category(bulk):
    c, ids = bulk
    r = ok(c.patch("/api/transactions", json={"ids": [ids["a"], ids["b"], ids["s"], ids["a"]], "category": "TRAVEL"}))
    assert [t["id"] for t in r["items"]] == [ids["a"], ids["b"]]
    assert all(t["category"] == "TRAVEL" and t["category_source"] == "user_bulk" and t["rule_id"] is None
               for t in r["items"])
    assert r["previous"] == [{"id": ids["a"], "category": "FOOD_AND_DRINK", "category_source": "plaid"},
                             {"id": ids["b"], "category": None, "category_source": "plaid"}]
    assert r["skipped"] == [ids["s"]]
    assert by_id(c)[ids["s"]]["category"] == "TRAVEL" and by_id(c)[ids["s"]]["category_source"] == "plaid"

    # One changing row is a plain "user" set; rows already set by hand to it don't change.
    r = ok(c.patch("/api/transactions", json={"ids": [ids["a"], ids["c"]], "category": "TRAVEL"}))
    assert [t["id"] for t in r["items"]] == [ids["c"]] and r["items"][0]["category_source"] == "user"
    assert r["previous"] == [{"id": ids["c"], "category": "OTHER", "category_source": "user"}]
    r = ok(c.patch("/api/transactions", json={"ids": [ids["a"]], "category": "TRAVEL"}))
    assert r == {"items": [], "previous": [], "skipped": []}


def test_bulk_validation_changes_nothing(bulk):
    c, ids = bulk
    before = {i: state(t) for i, t in by_id(c).items()}
    cases = [
        ({"ids": [ids["a"]], "category": "NOPE"}, 422),
        ({"ids": [ids["a"], 999999], "category": "TRAVEL"}, 404),
        ({"ids": [], "category": "TRAVEL"}, 422),
        ({"ids": list(range(1, 1002)), "category": "TRAVEL"}, 422),
        ({"ids": [ids["a"]], "category": "TRAVEL", "rule_id": 1}, 422),
        ({"ids": [ids["a"]], "category": "TRAVEL", "category_source": "rule"}, 422),
        ({"ids": [ids["a"]]}, 422),
        ({"ids": [ids["a"]], "category": ""}, 422),
        ({"ids": [ids["a"]], "category": "x" * 65}, 422),
        ({"ids": [0], "category": "TRAVEL"}, 422),
        ({"ids": [2**63], "category": "TRAVEL"}, 422),
    ]
    for body, status in cases:
        r = c.patch("/api/transactions", json=body)
        assert r.status_code == status, (body, r.text)
    assert c.patch("/api/transactions", json={"ids": [ids["a"], 999999], "category": "TRAVEL"}).json() == {
        "detail": "transaction not found"}
    assert c.patch("/api/transactions", json={"ids": [ids["a"]], "category": "NOPE"}).json() == {
        "detail": "unknown category"}
    assert {i: state(t) for i, t in by_id(c).items()} == before
    # 1000 ids is the limit, not an error by itself (they don't exist here: 404).
    assert c.patch("/api/transactions", json={"ids": list(range(1, 1001)), "category": "TRAVEL"}).status_code == 404


def test_bulk_reset_to_automatic_reapplies_rules(bulk):
    c, ids = bulk
    ok(c.patch("/api/transactions", json={"ids": [ids["a"], ids["b"], ids["c"]], "category": "MEDICAL"}))
    rule = ok(c.post("/api/rules", json={"field": "merchant", "op": "is", "text": "Coffee Shop",
                                         "action": "category", "category": "ENTERTAINMENT"}))
    assert by_id(c)[ids["a"]]["category"] == "MEDICAL"  # hand-set (user_bulk) survives the new rule
    r = ok(c.patch("/api/transactions", json={"ids": [ids["a"], ids["b"], ids["c"], ids["s"]], "category": None}))
    rows = {t["id"]: t for t in r["items"]}
    assert state(rows[ids["a"]]) == ("ENTERTAINMENT", "rule", rule["id"])
    assert state(rows[ids["b"]]) == (None, "plaid", None)
    assert state(rows[ids["c"]]) == ("OTHER", "plaid", None)
    assert r["skipped"] == [ids["s"]]
    assert [p["category_source"] for p in r["previous"]] == ["user_bulk"] * 3
    # Already automatic: nothing changes.
    assert ok(c.patch("/api/transactions", json={"ids": [ids["a"]], "category": None}))["items"] == []


# ------------------------------------------------------------------ 9. user_bulk is hand-set everywhere


def test_user_bulk_survives_rules_sync_and_counts_as_manual(unlocked, fake_plaid):
    from tests.test_plaid import BANK_TOKEN, acct, txn

    c = unlocked
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 100)]
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("a", "chk", 10, "Ride Share trip", category="TRANSPORTATION"),
                                      txn("b", "chk", 20, "Ride Share food", category="FOOD_AND_DRINK"),
                                      txn("c", "chk", 30, "Ride Share food", category="FOOD_AND_DRINK")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [],
         "modified": [txn("b", "chk", 21, "Ride Share food", category="GENERAL_MERCHANDISE"),
                      txn("c", "chk", 31, "Ride Share food", category="GENERAL_MERCHANDISE")],
         "removed": [], "next_cursor": "c2", "has_more": False},
    ]
    item = ok(c.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}))["item"]
    ok(c.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]}))
    food = [t["id"] for t in listed(c) if t["name"] == "Ride Share food"]
    r = ok(c.patch("/api/transactions", json={"ids": food, "category": "ENTERTAINMENT"}))
    assert {t["category_source"] for t in r["items"]} == {"user_bulk"}

    ok(c.post("/api/rules", json={"field": "name", "op": "contains", "text": "ride share", "action": "category",
                                  "category": "TRANSPORTATION"}))
    rows = by_id(c)
    assert all(state(rows[i]) == ("ENTERTAINMENT", "user_bulk", None) for i in food)

    ok(c.post("/api/plaid/sync", json={}))
    rows = by_id(c)
    assert all(rows[i]["plaid_category"] == "GENERAL_MERCHANDISE" for i in food)
    assert all(state(rows[i]) == ("ENTERTAINMENT", "user_bulk", None) for i in food)

    body = {"field": "name", "text": "ride share food", "category": "TRAVEL"}
    assert ok(c.post("/api/transactions/recategorize/preview", json=body)) == {"matches": 2, "already": 0,
                                                                                "manual": 2}
    assert ok(c.post("/api/transactions/recategorize", json={**body, "include_manual": False})) == {"changed": 0}
    assert all(by_id(c)[i]["category"] == "ENTERTAINMENT" for i in food)
    assert ok(c.post("/api/transactions/recategorize", json={**body, "include_manual": True})) == {"changed": 2}
    assert all(state(by_id(c)[i]) == ("TRAVEL", "user", None) for i in food)


# ------------------------------------------------------------------ 10. restore


def test_restore(bulk):
    c, ids = bulk
    rule = ok(c.post("/api/rules", json={"field": "name", "op": "is", "text": "book store",
                                         "action": "category", "category": "ENTERTAINMENT"}))
    ok(c.patch("/api/transactions", json={"ids": [ids["a"], ids["b"]], "category": "MEDICAL"}))
    r = ok(c.post("/api/transactions/restore", json={"items": [
        {"id": ids["a"], "category": "TRAVEL", "category_source": "user_bulk"},
        {"id": ids["b"], "category": "IGNORED_ANYWAY", "category_source": "rule"},
        {"id": ids["c"], "category": "FOOD_AND_DRINK", "category_source": "user"},
        {"id": ids["s"], "category": None, "category_source": "plaid"},
    ]}))
    rows = {t["id"]: t for t in r["items"]}
    assert list(rows) == [ids["a"], ids["b"], ids["c"]]  # the split row is skipped
    assert state(rows[ids["a"]]) == ("TRAVEL", "user_bulk", None)
    assert state(rows[ids["b"]]) == ("ENTERTAINMENT", "rule", rule["id"])  # rules decide again
    assert state(rows[ids["c"]]) == ("FOOD_AND_DRINK", "user", None)
    r = ok(c.post("/api/transactions/restore", json={"items": [
        {"id": ids["a"], "category": "whatever", "category_source": "plaid"}]}))
    assert state(r["items"][0]) == ("FOOD_AND_DRINK", "plaid", None)


def test_restore_validation_changes_nothing(bulk):
    c, ids = bulk
    before = {i: state(t) for i, t in by_id(c).items()}
    item = {"id": ids["a"], "category": "TRAVEL", "category_source": "user"}
    cases = [
        ({"items": [item, {**item, "category_source": "history"}]}, 422),
        ({"items": [item, {**item, "id": ids["b"], "category": "NOPE"}]}, 422),
        ({"items": [item, {**item, "id": ids["b"], "category": None}]}, 422),
        ({"items": [item, {**item, "id": ids["b"], "category_source": "user_bulk", "category": None}]}, 422),
        ({"items": [item, item]}, 422),
        ({"items": [item, {**item, "id": 999999}]}, 404),
        ({"items": [{**item, "rule_id": 1}]}, 422),
        ({"items": [item], "extra": True}, 422),
        ({"items": []}, 422),
        ({"items": [{"id": i, "category": None, "category_source": "plaid"} for i in range(1, 1002)]}, 422),
        ({"items": [{"id": ids["a"], "category_source": "user"}]}, 422),
        ({"items": [{**item, "category": "x" * 65}]}, 422),
    ]
    for body, status in cases:
        r = c.post("/api/transactions/restore", json=body)
        assert r.status_code == status, (body, r.text)
    assert {i: state(t) for i, t in by_id(c).items()} == before


# ------------------------------------------------------------------ 11. Undo end to end


def test_undo_puts_every_row_back(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    rows = [
        add_txn(db, acct.id, "2026-09-01", -1, "COFFEE 1", merchant="Coffee Shop", category=None),
        add_txn(db, acct.id, "2026-09-02", -2, "COFFEE 2", merchant="Coffee Shop", category="OTHER"),
        add_txn(db, acct.id, "2026-09-03", -3, "COFFEE 3", merchant="Coffee Shop", category="TRAVEL",
                category_source="user"),
        add_txn(db, acct.id, "2026-09-04", -4, "COFFEE 4", merchant="Coffee Shop", category="MEDICAL",
                category_source="user_bulk"),
        add_txn(db, acct.id, "2026-09-05", -5, "Book Store", category="OTHER"),
    ]
    ok(c.post("/api/rules", json={"field": "name", "op": "is", "text": "book store", "action": "category",
                                  "category": "ENTERTAINMENT"}))
    snapshot = {t["id"]: state(t) for t in listed(c)}
    assert snapshot[rows[4].id][1] == "rule"
    restore_items = [{"id": i, "category": s[0], "category_source": s[1]} for i, s in snapshot.items()]

    # Single set, then "Always for Coffee Shop", then remove that rule, then Undo.
    ok(c.patch(f"/api/transactions/{rows[0].id}", json={"category": "FOOD_AND_DRINK"}))
    created = ok(c.post("/api/rules", json={"field": "merchant", "op": "is", "text": "Coffee Shop",
                                            "action": "category", "category": "FOOD_AND_DRINK"}))
    assert created["matches"] == 1  # the OTHER one; hand-set rows are kept
    assert c.delete(f"/api/rules/{created['id']}").status_code == 204
    ok(c.post("/api/transactions/restore", json={"items": restore_items}))
    assert {t["id"]: state(t) for t in listed(c)} == snapshot

    # Bulk set with previous as the snapshot.
    r = ok(c.patch("/api/transactions", json={"ids": list(snapshot), "category": "GENERAL_SERVICES"}))
    ok(c.post("/api/transactions/restore", json={"items": r["previous"]}))
    assert {t["id"]: state(t) for t in listed(c)} == snapshot


# ------------------------------------------------------------------ 12. related


def test_related(unlocked, db, fixed_today):
    c = unlocked
    acct = make_account(db, "Checking", "bank", 100)
    savings = make_account(db, "Savings", "bank", 100)
    a = add_txn(db, acct.id, "2026-09-10", -4.5, "COFFEE #1", merchant="Coffee Shop")
    add_txn(db, acct.id, "2026-08-03", -3.25, "COFFEE #2", merchant="  coffee shop ")
    add_txn(db, savings.id, "2026-09-12", 10, "COFFEE REFUND", merchant="COFFEE SHOP")
    add_txn(db, acct.id, "2026-07-01", -99, "Coffee Shop", merchant="Coffee Shop Deluxe")  # not exact
    add_txn(db, acct.id, "2026-07-02", -99, "Coffee Shop", merchant=None)  # name, not merchant
    assert ok(c.get(f"/api/transactions/{a.id}/related")) == {
        "field": "merchant", "text": "Coffee Shop", "count": 3, "total": 2.25, "first_date": "2026-08-03"}

    b = add_txn(db, acct.id, "2026-09-01", -20, "  Book Store ", merchant=None)
    add_txn(db, acct.id, "2026-06-01", -5, "BOOK STORE", merchant="Something else")
    add_txn(db, acct.id, "2026-05-01", -5, "Book Store #2", merchant=None)
    assert ok(c.get(f"/api/transactions/{b.id}/related")) == {
        "field": "name", "text": "Book Store", "count": 2, "total": -25.0, "first_date": "2026-06-01"}
    assert c.get("/api/transactions/999999/related").status_code == 404
    assert c.get(f"/api/transactions/{2**63}/related").status_code == 422
