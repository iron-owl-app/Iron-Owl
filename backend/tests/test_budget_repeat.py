"""Release 3.17: budget plans repeat each month, and one-time moves (SPEC "Budget plans repeat
each month (Release 3.17)").

Today is 2026-09-26. Every test pins ``budget_plans_repeat_from`` itself (conftest keeps setup
and unlock from setting it). Numbers are worked out by hand from the fixture.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db as dbmod
from app import migrations
from app.models import AppSetting, Budget, Goal
from app.services import goal_links, spending
from tests.conftest import PASSWORD, REAL_START_PLANS_REPEAT, add_budget, add_txn, make_account
from tests.test_budget_bills import build_v6_vault
from tests.test_migrations import REAL_LATEST, columns, raw

FOOD, TRAVEL, FUN = "FOOD_AND_DRINK", "TRANSPORTATION", "ENTERTAINMENT"


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def get(client, month) -> dict:
    return ok(client.get("/api/budgets", params={"month": month}))


def save(client, month, assigned=None, removed=None, moved=None):
    body = {}
    if assigned is not None:
        body["assigned"] = assigned
    if removed is not None:
        body["removed"] = removed
    if moved is not None:
        body["moved"] = moved
    return client.put(f"/api/budgets/{month}", json=body)


def plans(b) -> dict[str, float]:
    return {c["category"]: c["assigned"] for c in b["categories"]}


def lines(b) -> dict[str, tuple]:
    """category -> (carryover, assigned, spent, available, moved)."""
    return {
        c["category"]: (c["carryover"], c["assigned"], c["spent"], c["available"], c["moved"]) for c in b["categories"]
    }


def month_only(b: dict) -> dict:
    """A month's view without the numbers that are the same on every month (Not planned yet is
    today's, so it moves when today's month starts repeating plans)."""
    return {k: v for k, v in b.items() if k not in ("ready_to_assign", "month_money")}


def pin(db, month: str | None) -> None:
    spending.put_setting(db, spending.BUDGET_PLANS_REPEAT_SETTING, month)
    db.commit()


def rows(db, category) -> list[tuple]:
    db.expire_all()
    return [
        (b.month, b.limit_cents, b.removed, b.moved_cents)
        for b in db.scalars(select(Budget).where(Budget.category == category).order_by(Budget.month))
    ]


@pytest.fixture
def env(unlocked, db, fixed_today):
    """$1000 in checking. July: Food 100 / Transportation 50 (rows); August: Food 120 (row);
    spending: Food 60 in July, 80 in August, 10 in September."""
    chk = make_account(db, "Checking", "bank", 1000)
    add_budget(db, "2026-07", {FOOD: 100, TRAVEL: 50})
    add_budget(db, "2026-08", {FOOD: 120})
    add_txn(db, chk.id, "2026-07-05", -60, "Groceries")
    add_txn(db, chk.id, "2026-08-05", -80, "Groceries")
    add_txn(db, chk.id, "2026-09-03", -10, "Coffee")
    return SimpleNamespace(client=unlocked, db=db, chk=chk)


# ------------------------------------------------------------------ the rule


def test_without_the_setting_nothing_repeats(env):
    """Absent setting = the rule before Release 3.17 (a new month starts at $0)."""
    sep = get(env.client, "2026-09")
    assert plans(sep) == {FOOD: 0.0, TRAVEL: 0.0}
    # Carryover only: Food 40 + 40 left, Transportation 50 left.
    assert lines(sep)[FOOD][:4] == (80.0, 0.0, 10.0, 70.0)
    assert sep["ready_to_assign"] == 1000 - 70 - 50


def test_plans_repeat_from_the_start_month(env):
    pin(env.db, "2026-09")
    sep = get(env.client, "2026-09")
    # Each category repeats its latest earlier row: Food August's 120, Transportation July's 50.
    assert plans(sep) == {FOOD: 120.0, TRAVEL: 50.0}
    assert lines(sep)[FOOD] == (80.0, 120.0, 10.0, 190.0, 0.0)
    # 1000 − (190 + 100) in September's envelopes; later months' repeats set nothing aside now.
    assert sep["ready_to_assign"] == 710.0
    assert sep["month_money"] == 710.0 + 170.0
    oct_ = get(env.client, "2026-10")
    assert plans(oct_) == {FOOD: 120.0, TRAVEL: 50.0}
    assert oct_["ready_to_assign"] == 710.0
    assert plans(get(env.client, "2027-09")) == {FOOD: 120.0, TRAVEL: 50.0}
    assert "suggested" not in sep
    # Viewing saves nothing.
    assert [r[0] for r in rows(env.db, FOOD)] == ["2026-07", "2026-08"]


def test_months_before_the_start_keep_their_numbers(env):
    """Start in August: July (before) is exactly what it was; August repeats nothing it has a
    row for, and Transportation (no August row) repeats July's 50 from August on."""
    before = month_only(get(env.client, "2026-07"))
    pin(env.db, "2026-08")
    assert month_only(get(env.client, "2026-07")) == before
    aug = get(env.client, "2026-08")
    assert plans(aug) == {FOOD: 120.0, TRAVEL: 50.0}
    # A start month in the future changes nothing that is shown today.
    pin(env.db, None)
    old = {m: get(env.client, m) for m in ("2026-07", "2026-08", "2026-09")}
    pin(env.db, "2026-10")
    for month, b in old.items():
        assert get(env.client, month) == b
    assert plans(get(env.client, "2026-10")) == {FOOD: 120.0, TRAVEL: 50.0}


def test_editing_a_month_changes_the_later_months_that_repeat(env):
    pin(env.db, "2026-09")
    client = env.client
    ok(save(client, "2026-09", {FOOD: 150}))
    assert plans(get(client, "2026-10"))[FOOD] == 150.0
    # Raising October above what it would repeat sets the extra aside now (50).
    b = ok(save(client, "2026-10", {FOOD: 200}))
    assert b["ready_to_assign"] == 1000 - (80 + 150 - 10) - 100 - 50
    assert plans(get(client, "2026-11"))[FOOD] == 200.0
    assert plans(get(client, "2026-09"))[FOOD] == 150.0
    # Lowering a later month frees nothing now.
    ready = b["ready_to_assign"]
    assert ok(save(client, "2026-12", {FOOD: 20}))["ready_to_assign"] == ready
    assert plans(get(client, "2027-01"))[FOOD] == 20.0
    assert plans(get(client, "2026-11"))[FOOD] == 200.0


def test_later_rows_saved_before_now_set_aside_only_their_raise(env):
    """Existing later-month rows: before Release 3.17 the whole row was set aside; now only the
    part above the repeated plan (this can change today's Not planned yet)."""
    add_budget(env.db, "2026-10", {FOOD: 120, TRAVEL: 80})
    old = get(env.client, "2026-09")["ready_to_assign"]
    assert old == 1000 - 70 - 50 - 200
    pin(env.db, "2026-09")
    new = get(env.client, "2026-09")["ready_to_assign"]
    # September now plans 120 + 50 itself; October sets aside Transportation's raise (30) only.
    assert new == 1000 - 190 - 100 - 30


def test_removal_stops_the_repeat_and_readding_starts_from_the_new_plan(env):
    pin(env.db, "2026-09")
    client = env.client
    ok(save(client, "2026-10", removed=[TRAVEL]))
    assert TRAVEL not in plans(get(client, "2026-11"))
    ok(save(client, "2026-11", {TRAVEL: 30}))
    assert plans(get(client, "2027-02"))[TRAVEL] == 30.0
    # Removed and re-added in the same month (a restart row): the new plan repeats.
    ok(save(client, "2026-09", removed=[FOOD]))
    assert FOOD not in plans(get(client, "2026-10"))
    ok(save(client, "2026-09", {FOOD: 70}))
    assert [(m, removed) for m, _, removed, _ in rows(env.db, FOOD)][-1] == ("2026-09", False)
    sep = get(client, "2026-09")
    assert lines(sep)[FOOD][0] == 0.0  # restart: no carryover
    assert plans(get(client, "2026-10"))[FOOD] == 70.0


def test_money_moved_out_and_money_already_had_repeat_as_zero(env):
    pin(env.db, "2026-09")
    client = env.client
    # Moving 30 of September's leftover out of Food (a negative plan) doesn't repeat.
    ok(save(client, "2026-09", {FOOD: -30}))
    assert plans(get(client, "2026-10"))[FOOD] == 0.0
    # Setup's "money you already had" row (August, Fun) isn't a plan either.
    add_budget(env.db, "2026-08", {FUN: 500})
    spending.put_setting(env.db, spending.BUDGET_SETUP_SEED_SETTING, json.dumps({"month": "2026-08", "category": FUN}))
    env.db.commit()
    assert plans(get(client, "2026-09"))[FUN] == 0.0


def test_goal_and_debt_categories_keep_their_own_plans(env):
    pin(env.db, "2026-09")
    db, client = env.db, env.client
    add_budget(db, "2026-08", {FUN: 999, "TRAVEL": 999})
    goal = Goal(name="Trip", kind="save", target_cents=60000, start_cents=0, current_cents=0, monthly_cents=9000)
    db.add(goal)
    db.flush()
    goal_links.store_links(db, {goal.id: goal_links.Link(FUN)})
    spending.put_setting(db, "debt_budget", json.dumps({
        "category": "TRAVEL", "linked": True, "extra_cents": 15000, "strategy": "avalanche", "account_ids": [],
    }))
    db.commit()
    sep = get(client, "2026-09")
    # The goal plans min(monthly 90, target 600 − saved 999 carried) = 0: reached.
    fun = next(c for c in sep["categories"] if c["category"] == FUN)
    assert fun["assigned"] == 0.0 and fun["goal"]["reached"] is True
    # The debt category plans its extra.
    assert plans(sep)["TRAVEL"] == 150.0
    goal.target_cents = 200000
    db.commit()
    assert plans(get(client, "2026-10"))[FUN] == 90.0


def test_repeated_plans_can_leave_not_planned_yet_below_zero(env):
    """More planned than the money: everything still repeats, Not planned yet goes negative;
    raising is refused, lowering is always allowed."""
    add_budget(env.db, "2026-08", {TRAVEL: 900})
    pin(env.db, "2026-09")
    client = env.client
    sep = get(client, "2026-09")
    assert plans(sep) == {FOOD: 120.0, TRAVEL: 900.0}
    # Transportation: 50 left in July + 900 in August + 900 repeated = 1850.
    assert sep["ready_to_assign"] == 1000 - 190 - 1850
    r = save(client, "2026-09", {FOOD: 121})
    assert r.status_code == 422 and r.json()["detail"] == "Only $0 is ready to assign. Lower another category first."
    assert ok(save(client, "2026-09", {TRAVEL: 100}))["ready_to_assign"] > sep["ready_to_assign"]


# ------------------------------------------------------------------ one-time moves


def test_moves_are_one_time(env):
    pin(env.db, "2026-09")
    client = env.client
    # October: move 50 from Transportation to Food.
    b = ok(save(client, "2026-10", {FOOD: 170, TRAVEL: 0}, moved={FOOD: 50, TRAVEL: -50}))
    assert lines(b)[FOOD] == (190.0, 170.0, 0.0, 360.0, 50.0)
    assert lines(b)[TRAVEL] == (100.0, 0.0, 0.0, 100.0, -50.0)
    assert b["ready_to_assign"] == 710.0  # a move sets nothing aside
    assert plans(get(client, "2026-11")) == {FOOD: 120.0, TRAVEL: 50.0}
    assert rows(env.db, FOOD)[-1] == ("2026-10", 17000, False, 5000)
    # Typing a plan folds that month's moves into it: the typed number repeats.
    ok(save(client, "2026-10", {FOOD: 160}))
    assert rows(env.db, FOOD)[-1] == ("2026-10", 16000, False, 0)
    assert plans(get(client, "2026-11"))[FOOD] == 160.0


def test_a_move_needs_the_new_amount_too(env):
    r = save(env.client, "2026-09", {FOOD: 10}, moved={TRAVEL: 5})
    assert r.status_code == 422 and r.json()["detail"] == "A one-time move needs the category's new amount too."


def test_exact_undo_rows_carry_moves(env):
    pin(env.db, "2026-09")
    client = env.client
    ok(save(client, "2026-09", {FOOD: 170, TRAVEL: 0}, moved={FOOD: 50, TRAVEL: -50}))
    state = ok(client.get(f"/api/budgets/2026-09/rows/{FOOD}"))["state"]
    assert state == {"assigned": 170.0, "removed": False, "restart": False, "moved": 50.0}
    ok(save(client, "2026-09", {FOOD: 200}))
    after = ok(client.get(f"/api/budgets/2026-09/rows/{FOOD}"))["state"]
    ok(client.put("/api/budgets/2026-09/rows", json={"rows": [{"category": FOOD, "state": state, "expected": after}]}))
    assert rows(env.db, FOOD)[-1] == ("2026-09", 17000, False, 5000)
    # A state without ``moved`` (an older page) means 0.
    r = client.put("/api/budgets/2026-09/rows", json={"rows": [{
        "category": FOOD, "state": None, "expected": {"assigned": 170.0, "removed": False, "restart": False},
    }]})
    assert r.status_code == 409


# ------------------------------------------------------------------ Reports and review


def test_reports_planned_reports_the_repeated_plan(env):
    pin(env.db, "2026-09")
    months = {m["month"]: m for m in ok(env.client.get("/api/reports/monthly", params={"months": 3}))["months"]}
    assert months["2026-07"]["planned"] == {FOOD: 100.0, TRAVEL: 50.0}
    assert months["2026-08"]["planned"] == {FOOD: 120.0}  # before the start: its own rows
    assert months["2026-09"]["planned"] == {FOOD: 120.0, TRAVEL: 50.0}
    history = {h["month"]: h["assigned"] for h in ok(env.client.get("/api/spending/review", params={"month": "2026-09"}))["history"]}
    assert (history["2026-08"], history["2026-09"]) == (120.0, 170.0)


# ------------------------------------------------------------------ the start month


def test_start_month_is_set_once(unlocked, db, fixed_today):
    assert spending.plans_repeat_from(db) is None
    assert spending.start_plans_repeat(db, fixed_today()) == "2026-09"
    fixed_today.set(fixed_today().replace(month=11))
    assert spending.start_plans_repeat(db, fixed_today()) == "2026-09"
    spending.put_setting(db, spending.BUDGET_PLANS_REPEAT_SETTING, "junk")
    assert spending.plans_repeat_from(db) is None


def test_unlock_sets_the_start_month(unlocked, db, vault, fixed_today, monkeypatch):
    monkeypatch.setattr(spending, "safe_start_plans_repeat", REAL_START_PLANS_REPEAT)
    unlocked.post("/api/auth/lock")
    assert ok(unlocked.post("/api/auth/unlock", json={"password": PASSWORD}))
    session = vault.db.acquire()
    try:
        assert spending.plans_repeat_from(session) == "2026-09"
    finally:
        vault.db.release(session)


# ------------------------------------------------------------------ migration v8


def test_v7_vault_upgrades_to_v8_with_moves_at_zero(settings, client, monkeypatch, fixed_today):
    key = build_v6_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 7)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "moved_cents" not in columns(conn, "budgets")
        conn.execute("DELETE FROM budgets WHERE month = '2026-05'")
        conn.execute(
            "INSERT INTO budgets (month, category, limit_cents, removed, restart) VALUES ('2026-05', ?, 12345, 0, 0)",
            (FOOD,),
        )
        conn.commit()
        count = conn.execute("SELECT count(*) FROM budgets").fetchone()[0]
    finally:
        conn.close()
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    client.post("/api/auth/lock")
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST == 8
        assert columns(conn, "budgets")["moved_cents"] == (1, "0")
        assert conn.execute("SELECT count(*) FROM budgets WHERE moved_cents = 0").fetchone()[0] == count
        assert conn.execute(
            "SELECT limit_cents FROM budgets WHERE month = '2026-05' AND category = ?", (FOOD,)
        ).fetchone() == (12345,)
    finally:
        conn.close()
    names = [p.name for p in settings.data_dir.iterdir() if p.name.startswith("pre-migrate-")]
    assert any(n.startswith("pre-migrate-v7-") for n in names)  # the safety copy was made


def test_failed_v8_migration_rolls_back_to_v7(settings, monkeypatch):
    key = build_v6_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 7)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)

    def broken(conn):
        conn.execute("ALTER TABLE budgets ADD COLUMN moved_cents INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nope ADD COLUMN x TEXT")

    monkeypatch.setitem(migrations.MIGRATIONS, 8, broken)
    database = dbmod.Database(settings.db_path)
    with pytest.raises(Exception):  # noqa: B017 - sqlcipher3.OperationalError
        database.open(bytearray(key))
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "moved_cents" not in columns(conn, "budgets")
    finally:
        conn.close()


def test_settings_row_is_plain(unlocked, db, fixed_today):
    """The start month is a plain app_settings value (no migration)."""
    spending.start_plans_repeat(db, fixed_today())
    db.commit()
    assert db.get(AppSetting, "budget_plans_repeat_from").value == "2026-09"


# ------------------------------------------------------------------ money review (rule A, exactly)


def test_a_move_to_ready_to_assign_from_a_later_month_frees_nothing(env):
    """November Food repeats 120, none of it set aside now: moving 100 of it to Not planned
    yet (a one-sided move) frees nothing, like typing 20."""
    pin(env.db, "2026-09")
    c = env.client
    assert get(c, "2026-09")["ready_to_assign"] == 710.0
    b = ok(save(c, "2026-11", {FOOD: 20}, moved={FOOD: -100}))
    assert b["ready_to_assign"] == 710.0 and plans(b)[FOOD] == 20.0
    assert plans(get(c, "2026-12"))[FOOD] == 120.0  # the move was one-time
    ok(save(c, "2026-11", {FOOD: 120}))
    assert ok(save(c, "2026-11", {FOOD: 20}))["ready_to_assign"] == 710.0


def test_changing_the_debt_extra_or_a_goal_never_rewrites_a_past_month(env):
    pin(env.db, "2026-07")
    db, c = env.db, env.client

    def debt(extra_cents):
        spending.put_setting(db, "debt_budget", json.dumps({
            "category": TRAVEL, "linked": True, "extra_cents": extra_cents, "strategy": "avalanche", "account_ids": [],
        }))
        db.commit()

    debt(5000)
    # August (over) gets the extra's plan as a row of its own (start July, as if it ran then).
    spending.safe_freeze_rule_months(db, date(2026, 9, 26))
    aug = lines(get(c, "2026-08"))
    assert {k: v[1] for k, v in aug.items()} == {FOOD: 120.0, TRAVEL: 50.0}
    debt(40000)
    assert lines(get(c, "2026-08")) == aug
    assert plans(get(c, "2026-09"))[TRAVEL] == 400.0  # today's month on: the extra
    # A goal attached later doesn't touch August either (it repeats July's row).
    add_budget(db, "2026-07", {FUN: 30})
    aug = lines(get(c, "2026-08"))
    goal = Goal(name="Trip", kind="save", target_cents=100000, start_cents=0, current_cents=0, monthly_cents=9000)
    db.add(goal)
    db.flush()
    goal_links.store_links(db, {goal.id: goal_links.Link(FUN)})
    db.commit()
    assert lines(get(c, "2026-08")) == aug and aug[FUN][1] == 30.0
    assert plans(get(c, "2026-09"))[FUN] == 90.0


def test_lowering_a_later_month_is_never_refused(env):
    pin(env.db, "2026-09")
    c = env.client
    assert ok(save(c, "2026-12", {FOOD: 120}))["ready_to_assign"] == 710.0  # no raise: nothing set aside
    ok(save(c, "2026-09", {TRAVEL: 50 + 710}))
    assert get(c, "2026-09")["ready_to_assign"] == 0.0
    b = ok(save(c, "2026-11", {FOOD: 0}))
    assert b["ready_to_assign"] == 0.0 and plans(b)[FOOD] == 0.0
    assert plans(get(c, "2026-12"))[FOOD] == 120.0


def test_moving_leftover_out_of_a_later_month_frees_only_money_here_now(env):
    """December's Food leftover 430 = 190 left today + October's and November's repeated 120s
    (that months' money): moving it all out frees only the 190."""
    pin(env.db, "2026-09")
    c = env.client
    dec = lines(get(c, "2026-12"))[FOOD]
    assert dec == (430.0, 120.0, 0.0, 550.0, 0.0)
    b = ok(save(c, "2026-12", {FOOD: -430}))
    assert b["ready_to_assign"] == 710.0 + 190.0


def test_a_later_raise_then_a_move_out_frees_what_was_set_aside(env):
    pin(env.db, "2026-09")
    c = env.client
    assert ok(save(c, "2026-10", {FOOD: 220}))["ready_to_assign"] == 610.0  # 100 set aside
    # November: move 150 of Food to Not planned yet. Its own repeated 220 isn't here now;
    # nothing more to free (moves come out of the month's own plan first).
    assert ok(save(c, "2026-11", {FOOD: 70}, moved={FOOD: -150}))["ready_to_assign"] == 610.0
    # Moving 400 out of December's leftover (190 today + 100 set aside + repeated plans) frees 290.
    assert ok(save(c, "2026-12", {FOOD: -400}))["ready_to_assign"] == 610.0 + 290.0


def test_a_move_between_categories_in_a_later_month_takes_nothing(env):
    pin(env.db, "2026-09")
    b = ok(save(env.client, "2026-11", {FOOD: 170, TRAVEL: 0}, moved={FOOD: 50, TRAVEL: -50}))
    assert b["ready_to_assign"] == 710.0
    # 80 in while only Transportation's 50 goes out: the other 30 comes from Not planned yet now.
    b = ok(save(env.client, "2026-11", {FOOD: 200}, moved={FOOD: 80}))
    assert b["ready_to_assign"] == 710.0 - 30.0


def test_already_saved_adds_to_a_repeated_plan(env):
    """A goal's "Already saved" row in last month keeps that month's repeated plan."""
    pin(env.db, "2026-07")
    db, c = env.db, env.client
    add_budget(db, "2026-07", {FUN: 100})
    spending.put_setting(db, spending.BUDGET_SAVINGS_SETTING, FUN)
    db.commit()
    assert plans(get(c, "2026-08"))[FUN] == 100.0
    r = c.post("/api/goals", json={"kind": "emergency", "name": "Rainy day", "target": 5000, "monthly": 100,
                                   "already_saved": 200})
    assert r.status_code == 201, r.text
    assert [row for row in rows(db, FUN) if row[0] == "2026-08"] == [("2026-08", 30000, False, 0)]


# ------------------------------------------------------------------ key rules with plans repeating


def test_over_assign_rule_with_repeat_on(env):
    pin(env.db, "2026-09")
    c = env.client
    r = save(c, "2026-09", {FOOD: 120 + 710.01})
    assert r.status_code == 422 and r.json()["detail"] == "Only $710 is ready to assign. Lower another category first."
    assert ok(save(c, "2026-09", {FOOD: 830}))["ready_to_assign"] == 0.0


def test_carryover_with_repeat_on(env):
    pin(env.db, "2026-09")
    add_txn(env.db, env.chk.id, "2026-09-10", -150, "Train pass", category=TRAVEL)
    c = env.client
    sep = get(c, "2026-09")
    assert lines(sep)[TRAVEL][:4] == (50.0, 50.0, 150.0, -50.0)
    oct_ = get(c, "2026-10")
    # Food's leftover carries; Transportation's overspending doesn't (it came out of Not planned yet).
    assert lines(oct_)[FOOD][:4] == (190.0, 120.0, 0.0, 310.0)
    assert lines(oct_)[TRAVEL][:4] == (0.0, 50.0, 0.0, 50.0)
    assert sep["ready_to_assign"] == 1000 - 190 + 50


def test_cover_with_repeat_on(env):
    pin(env.db, "2026-09")
    add_txn(env.db, env.chk.id, "2026-09-10", -150, "Train pass", category=TRAVEL)
    c = env.client
    before = get(c, "2026-09")["ready_to_assign"]
    b = ok(save(c, "2026-09", {TRAVEL: 100, FOOD: 70}, moved={TRAVEL: 50, FOOD: -50}))
    assert lines(b)[TRAVEL][3] == 0.0 and b["ready_to_assign"] == before
    assert plans(get(c, "2026-10")) == {FOOD: 120.0, TRAVEL: 50.0}


# ------------------------------------------------------------------ months that ended keep their goal and debt plans


def _goal_and_debt(db, target_cents=1_000_000, monthly_cents=30000, extra_cents=40000):
    """Fun (in the Budget since August at $100) becomes a goal's category; Transportation is
    the "Paying off debt" category."""
    add_budget(db, "2026-08", {FUN: 100})
    goal = Goal(name="Trip", kind="save", target_cents=target_cents, start_cents=0, current_cents=0,
                monthly_cents=monthly_cents)
    db.add(goal)
    db.flush()
    goal_links.store_links(db, {goal.id: goal_links.Link(FUN)})
    _debt(db, extra_cents)
    return goal


def _debt(db, extra_cents):
    spending.put_setting(db, "debt_budget", json.dumps({
        "category": TRAVEL, "linked": True, "extra_cents": extra_cents, "strategy": "avalanche", "account_ids": [],
    }))
    db.commit()


def test_a_month_that_ended_keeps_its_goal_and_debt_plans(env, fixed_today):
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    goal = _goal_and_debt(db)
    sep = get(c, "2026-09")
    assert plans(sep)[FUN] == 300.0 and plans(sep)[TRAVEL] == 400.0
    oct_before = lines(get(c, "2026-10"))
    fixed_today.set(fixed_today().replace(month=10, day=2))
    assert lines(get(c, "2026-09")) == lines(sep)
    oct_ = lines(get(c, "2026-10"))
    assert (oct_[FUN][:2], oct_[TRAVEL][:2]) == (oct_before[FUN][:2], oct_before[TRAVEL][:2])
    # Changing the goal or the extra now (the routes freeze September first) leaves September alone.
    ok(c.patch(f"/api/goals/{goal.id}", json={"monthly": 50}))
    _debt(db, 10000)
    assert plans(get(c, "2026-09")) == plans(sep)
    assert plans(get(c, "2026-10"))[FUN] == 50.0 and plans(get(c, "2026-10"))[TRAVEL] == 100.0
    assert [r[:2] for r in rows(db, FUN) if r[0] == "2026-09"] == [("2026-09", 30000)]
    assert [r[:2] for r in rows(db, TRAVEL) if r[0] == "2026-09"] == [("2026-09", 40000)]


def test_a_reached_goal_keeps_planning_zero_in_the_month_that_ended(env, fixed_today):
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    _goal_and_debt(db, target_cents=5000)  # $50: August's $100 leftover already reaches it
    assert plans(get(c, "2026-09"))[FUN] == 0.0
    fixed_today.set(fixed_today().replace(month=10, day=2))
    assert plans(get(c, "2026-09"))[FUN] == 0.0  # not August's $100 row


def test_months_skipped_while_closed_are_frozen_once(env, fixed_today):
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    goal = _goal_and_debt(db)
    sep, oct_ = plans(get(c, "2026-09")), plans(get(c, "2026-10"))
    fixed_today.set(fixed_today().replace(month=11, day=5))  # not opened in October
    assert plans(get(c, "2026-09")) == sep and plans(get(c, "2026-10")) == oct_
    spending.safe_freeze_rule_months(db, fixed_today())
    db.expire_all()
    assert db.get(AppSetting, spending.BUDGET_RULES_FROZEN_SETTING).value == "2026-10"
    frozen = [r for r in rows(db, FUN) + rows(db, TRAVEL) if r[0] in ("2026-09", "2026-10")]
    assert sorted(frozen) == sorted([
        ("2026-09", 30000, False, 0), ("2026-10", 30000, False, 0),
        ("2026-09", 40000, False, 0), ("2026-10", 40000, False, 0),
    ])
    # Idempotent: running again writes nothing more.
    spending.safe_freeze_rule_months(db, fixed_today())
    assert len(rows(db, FUN)) == 3 and len(rows(db, TRAVEL)) == 3  # July/August rows + the two frozen
    # Later goal and debt changes leave both months alone; November follows them.
    goal.monthly_cents = 5000
    _debt(db, 10000)
    assert plans(get(c, "2026-09")) == sep and plans(get(c, "2026-10")) == oct_
    assert plans(get(c, "2026-11"))[FUN] == 50.0 and plans(get(c, "2026-11"))[TRAVEL] == 100.0


def test_frozen_rows_repeat_only_when_the_rule_stops(env, fixed_today):
    """A frozen month is the plan it showed: if the category stops being the debt category,
    later months repeat that plan (last month's), like any other row."""
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    _debt(db, 40000)
    fixed_today.set(fixed_today().replace(month=10, day=2))
    assert plans(get(c, "2026-10"))[TRAVEL] == 400.0
    spending.safe_freeze_rule_months(db, fixed_today())
    spending.put_setting(db, "debt_budget", None)
    db.commit()
    assert plans(get(c, "2026-09"))[TRAVEL] == 400.0
    assert plans(get(c, "2026-10"))[TRAVEL] == 400.0


def test_re_adding_in_a_later_month_up_to_the_plan_so_far_takes_nothing(env):
    pin(env.db, "2026-09")
    c = env.client
    ok(save(c, "2026-11", removed=[FOOD]))
    assert get(c, "2026-09")["ready_to_assign"] == 710.0
    assert ok(save(c, "2026-11", {FOOD: 120}))["ready_to_assign"] == 710.0
    assert ok(save(c, "2026-11", {FOOD: 150}))["ready_to_assign"] == 680.0  # the raise above 120


@pytest.mark.parametrize("touch", ["category", "group", "delete_category"])
def test_category_and_group_changes_freeze_first(env, fixed_today, touch):
    """The first change after a month ended that can change which category a goal or debt
    uses (category edit or delete, group delete) freezes the month first (committed)."""
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    _goal_and_debt(db)
    fixed_today.set(fixed_today().replace(month=10, day=2))
    if touch == "category":
        c.patch(f"/api/categories/{FUN}", json={"hidden": False})
    elif touch == "group":
        group = c.post("/api/category-groups", json={"name": "Spare"}).json()
        c.delete(f"/api/category-groups/{group['id']}")
    else:
        c.delete(f"/api/categories/{FOOD}")
    db.expire_all()
    assert db.get(AppSetting, spending.BUDGET_RULES_FROZEN_SETTING).value == "2026-09"
    assert [r[:2] for r in rows(db, FUN) if r[0] == "2026-09"] == [("2026-09", 30000)]
    assert [r[:2] for r in rows(db, TRAVEL) if r[0] == "2026-09"] == [("2026-09", 40000)]


# ------------------------------------------------------------------ reads never write (no write lock)


def test_reads_freeze_in_memory_only(env, fixed_today, vault):
    """After a month ended, a read shows the frozen plans but writes nothing: two reads at once
    are fine and a save while a read is still open goes through right away."""
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    _goal_and_debt(db)
    sep = plans(get(c, "2026-09"))
    fixed_today.set(fixed_today().replace(month=10, day=2))
    count = len(rows(db, FUN)) + len(rows(db, TRAVEL))
    s1, s2 = vault.db.acquire(), vault.db.acquire()
    try:
        book1 = spending.load_book(s1, spending.load_categories(s1), fixed_today())
        book2 = spending.load_book(s2, spending.load_categories(s2), fixed_today())
        assert book1.lines["2026-09"][FUN].assigned == book2.lines["2026-09"][FUN].assigned == 30000
        assert not s1.new and not s1.dirty and not s2.new and not s2.dirty
        assert plans(get(c, "2026-09")) == sep
        assert ok(save(c, "2026-10", {FOOD: 50}))["month"] == "2026-10"  # no lock held by the reads
        s1.commit()
        s2.commit()
    finally:
        vault.db.release(s1)
        vault.db.release(s2)
    assert len(rows(db, FUN)) + len(rows(db, TRAVEL)) == count  # reads wrote no rows
    db.expire_all()
    assert db.get(AppSetting, spending.BUDGET_RULES_FROZEN_SETTING) is None


def test_the_automation_loop_freezes_once_a_month(env, fixed_today, app):
    from app.services import automation

    pin(env.db, "2026-09")
    db = env.db
    _goal_and_debt(db)
    state = app.state.fintrack
    assert automation.freeze_tick(state) is True  # September: nothing ended yet
    fixed_today.set(fixed_today().replace(month=10, day=2))
    assert automation.freeze_tick(state) is True
    assert automation.freeze_tick(state) is False  # once a month
    db.expire_all()
    assert db.get(AppSetting, spending.BUDGET_RULES_FROZEN_SETTING).value == "2026-09"
    assert [r[:2] for r in rows(db, FUN) if r[0] == "2026-09"] == [("2026-09", 30000)]


def test_a_change_is_refused_when_the_freeze_cant_be_saved(env, fixed_today, monkeypatch):
    pin(env.db, "2026-09")
    db, c = env.db, env.client
    goal = _goal_and_debt(db)
    fixed_today.set(fixed_today().replace(month=10, day=2))

    def broken(session, today):
        raise RuntimeError("disk full")

    monkeypatch.setattr(spending, "freeze_rule_months", broken)
    busy = {"detail": "Iron Owl is busy. Try again in a moment."}
    for r in (
        c.patch(f"/api/goals/{goal.id}", json={"monthly": 50}),
        c.delete("/api/debt/budget"),
        c.patch(f"/api/categories/{FUN}", json={"name": "Fun money"}),
        c.delete(f"/api/categories/{FOOD}"),
    ):
        assert (r.status_code, r.json()) == (409, busy)
    db.expire_all()
    assert db.get(Goal, goal.id).monthly_cents == 30000
    assert json.loads(spending.get_setting(db, "debt_budget"))["extra_cents"] == 40000
    assert db.get(spending.TxnCategory, FUN).name != "Fun money"
    assert db.get(spending.TxnCategory, FOOD) is not None
    assert db.get(AppSetting, spending.BUDGET_RULES_FROZEN_SETTING) is None
