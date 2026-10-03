"""Investments (D9, Release 3.8): classification, GET /api/investments and security_info."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app.models import AppSetting, BalanceSnapshot, Holding, PlaidItem
from app.services import sync as sync_service
from app.services.investments import classify, load_security_info
from tests.conftest import BASE_URL, CSRF, make_account

D = dt.date.fromisoformat


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(("name", "ticker", "info", "expected"), [
    ("US Dollar", "CUR:USD", {"type": "cash"}, ("cash", None)),
    ("Fidelity Government Money Market", "SPAXX", {"type": "mutual fund", "cash": True}, ("cash", None)),
    ("Schwab Cash Sweep", None, None, ("cash", None)),
    ("Pending settlement", None, {"type": "other"}, ("cash", None)),
    ("Anything", "CUR:EUR", None, ("cash", None)),
    ("Vanguard Target Retirement 2045 Fund", "VTIVX", {"type": "mutual fund"}, ("target_date", 2045)),
    ("Fidelity Freedom 2030", None, None, ("target_date", 2030)),
    ("BlackRock LifePath Index 2055", None, None, ("target_date", 2055)),
    ("Target 2040 Bond Blend", None, None, ("target_date", 2040)),  # first match wins
    ("Retirement Income Fund", None, {"type": "mutual fund"}, ("target_date", None)),  # no year
    ("Vanguard Total Bond Market Index", "BND", {"type": "etf"}, ("bond", None)),
    ("US Treasury Note 2031", None, {"type": "fixed income"}, ("bond", None)),
    ("iShares TIPS Bond ETF", "TIP", {"type": "etf"}, ("bond", None)),
    ("Stable Value Fund", None, None, ("bond", None)),
    ("Municipal Income", None, None, ("bond", None)),
    ("US Aggregate", None, None, ("bond", None)),
    ("Vanguard Total International Stock Index", "VXUS", {"type": "etf"}, ("intl_stock", None)),
    ("Emerging Markets Equity", None, {"type": "mutual fund"}, ("intl_stock", None)),
    ("Stocks ex-US", None, None, ("intl_stock", None)),
    ("Europe Pacific Growth", None, None, ("intl_stock", None)),
    ("Vanguard 500 Index Admiral", "VFIAX", {"type": "mutual fund"}, ("us_stock", None)),
    ("Total Stock Market", None, None, ("us_stock", None)),
    ("Growth Opportunities", "XYZ", {"type": "etf"}, ("us_stock", None)),
    ("Apple Inc.", "AAPL", {"type": "equity"}, ("company_stock", None)),
    ("Bitcoin", "BTC", {"type": "cryptocurrency"}, ("other", None)),
    ("Mystery holding", None, None, ("other", None)),
    # Review fixes (M6): Plaid's "equity" type wins over words in a company's name.
    ("Union Pacific Corp", "UNP", {"type": "equity"}, ("company_stock", None)),
    ("PG&E Corp (Pacific Gas & Electric)", "PCG", {"type": "equity"}, ("company_stock", None)),
    ("Cash America International", "CSH", {"type": "equity"}, ("company_stock", None)),
    ("Treasury Wine Estates", "TSRYY", {"type": "equity"}, ("company_stock", None)),
    ("Target Corp", "TGT", {"type": "equity"}, ("company_stock", None)),
    # A retirement date fund without a year in its name.
    ("Vanguard Target Retirement Income Fund", "VTINX", {"type": "mutual fund"}, ("target_date", None)),
    # Funds that say nothing specific are "other", not US stocks; so are gold and crypto.
    ("Vanguard Real Estate Index Fund", "VGSLX", {"type": "mutual fund"}, ("other", None)),
    ("Vanguard Wellington Fund", "VWELX", {"type": "mutual fund"}, ("other", None)),
    ("SPDR Gold Shares", "GLD", {"type": "etf"}, ("other", None)),
    ("iShares Bitcoin Trust ETF", "IBIT", {"type": "etf"}, ("other", None)),
    ("iShares Silver Trust", "SLV", {"type": "etf"}, ("other", None)),
    # Before the first sync after the upgrade (no security_info yet).
    ("Microsoft Corp", "MSFT", None, ("company_stock", None)),
    ("Vanguard Total Stock Market ETF", "VTI", None, ("us_stock", None)),
    ("Vanguard Target Retirement 2045", "VTIVX", None, ("target_date", 2045)),
    ("iShares Core US Aggregate Bond ETF", "AGG", None, ("bond", None)),
    # Still right.
    ("Fidelity 500 Index Fund", "FXAIX", {"type": "mutual fund"}, ("us_stock", None)),
    ("Cash", None, None, ("cash", None)),
])
def test_classify(name, ticker, info, expected):
    assert classify(name, ticker, info) == expected


EQ_ETF = {"type": "equity", "subtype": "etf"}
MF = {"type": "mutual fund"}


@pytest.mark.parametrize(("name", "ticker", "info", "expected"), [
    # L2 (review 2): Plaid types many ETFs "equity"; a fund subtype or fund words in the name
    # make it a fund, sorted by what it holds, not "company stock".
    ("Vanguard Total Stock Market ETF", "VTI", EQ_ETF, "us_stock"),
    ("SPDR S&P 500 ETF Trust", "SPY", {"type": "equity"}, "us_stock"),
    ("Invesco QQQ Trust", "QQQ", {"type": "equity"}, "us_stock"),
    ("Invesco QQQ Trust, Series 1", "QQQ", None, "us_stock"),
    ("iShares Core S&P Total U.S. Stock Market ETF", "ITOT", {"type": "equity"}, "us_stock"),
    ("Schwab U.S. Broad Market ETF", "SCHB", EQ_ETF, "us_stock"),
    ("Vanguard Dividend Appreciation ETF", "VIG", EQ_ETF, "us_stock"),
    ("Vanguard Total International Stock ETF", "VXUS", EQ_ETF, "intl_stock"),
    ("Vanguard Total Bond Market ETF", "BND", EQ_ETF, "bond"),  # bonds stay bonds
    ("Vanguard Total Bond Market ETF", "BND", {"type": "etf"}, "bond"),
    ("Vanguard Total Bond Market ETF", "BND", None, "bond"),
    ("iShares Gold Trust", "IAU", {"type": "equity"}, "other"),
    # Plaid's subtype string for a REIT (plaid-python Security.subtype).
    ("Realty Income Corp", "O", {"type": "equity", "subtype": "real estate investment trust"}, "other"),
    ("Vanguard S&P 500 ETF", "VOO", {"type": "equity", "subtype": "mutual fund"}, "us_stock"),
    ("Fund of funds growth", None, {"type": "equity", "subtype": "fund of funds"}, "us_stock"),
    # Companies stay companies, "Trust" in a company's name included.
    ("Northern Trust Corp", "NTRS", {"type": "equity"}, "company_stock"),
    ("Apple Inc.", "AAPL", {"type": "equity", "subtype": "common stock"}, "company_stock"),
    ("Value Line Inc", "VALU", {"type": "equity"}, "company_stock"),
    # More names that say US stocks.
    ("Vanguard Value ETF", "VTV", {"type": "etf"}, "us_stock"),
    ("Vanguard Value Index Fund", "VVIAX", MF, "us_stock"),
    ("T. Rowe Price Blue Chip Growth", "TRBCX", MF, "us_stock"),
    ("Vanguard Equity Income Fund", "VEIPX", MF, "us_stock"),
    ("Schwab Broad Market Index", None, MF, "us_stock"),
    ("Invesco NASDAQ-100 Index Fund", "QQQM", MF, "us_stock"),
    ("Vanguard Mega Cap ETF", "MGC", {"type": "etf"}, "us_stock"),
    ("Vanguard Institutional Index Fund", "VINIX", MF, "us_stock"),
    ("Fidelity Total Market Index", "FSKAX", MF, "us_stock"),
    ("Fidelity ZERO Total Market Index", None, None, "us_stock"),
    ("Schwab S&P 500 Index", "SWPPX", MF, "us_stock"),
    ("State Street 500 Index Securities Lending Series", None, None, "us_stock"),
    # A fund that says it holds stocks (and isn't international or bonds): US stocks.
    ("Fidelity Stock Selector All Cap Fund", "FDSSX", MF, "us_stock"),
    ("Fidelity Equity Fund", None, MF, "us_stock"),
    ("Fidelity International Equity Fund", None, MF, "intl_stock"),
    # "Contrafund" is a fund's name ("fund" at the end of a word), not a company.
    ("Fidelity Contrafund", "FCNTX", None, "other"),
    ("Fidelity Contrafund", "FCNTX", MF, "other"),
    ("Fidelity Contrafund Stock Fund", None, None, "us_stock"),
    # Review 3: world and global funds hold US and international stocks together ("other",
    # never US stocks through the stock-words fallback); EuroPacific is international.
    ("Vanguard Total World Stock ETF", "VT", {"type": "etf"}, "other"),
    ("Vanguard Total World Stock ETF", "VT", {"type": "equity"}, "other"),
    ("Vanguard Total World Stock ETF", "VT", EQ_ETF, "other"),
    ("Vanguard Total World Stock ETF", "VT", None, "other"),
    ("Vanguard Total World Stock Index Admiral", "VTWAX", MF, "other"),
    ("Vanguard Global Equity Fund", "VHGEX", MF, "other"),
    ("iShares MSCI ACWI ETF", "ACWI", EQ_ETF, "other"),
    ("Vanguard FTSE All-World ex-US ETF", "VEU", EQ_ETF, "intl_stock"),
    ("American Funds EuroPacific Growth", "AEPGX", MF, "intl_stock"),
    ("American Funds EuroPacific Growth", None, None, "intl_stock"),
    # Plaid subtype strings that aren't funds leave an equity a company's stock.
    ("Some Holding Co", "SHC", {"type": "equity", "subtype": "preferred equity"}, "company_stock"),
    # Still right.
    ("Stable Value Fund", None, None, "bond"),
    ("Vanguard Wellington Fund", "VWELX", MF, "other"),
    ("Target Corp", "TGT", {"type": "equity"}, "company_stock"),
])
def test_classify_equity_typed_funds_and_us_stock_names(name, ticker, info, expected):
    assert classify(name, ticker, info) == (expected, None)


# ------------------------------------------------------------------ GET /api/investments


def snap(db, account, day, dollars, estimated=False):
    db.add(BalanceSnapshot(account_id=account.id, date=D(day), balance_cents=round(dollars * 100), estimated=estimated))
    db.commit()


def hold(db, account, security_id, name, dollars, ticker=None):
    db.add(Holding(account_id=account.id, security_id=security_id, name=name, ticker=ticker, quantity=1.0,
                   price=dollars, value_cents=round(dollars * 100)))
    db.commit()


@pytest.fixture
def portfolio(unlocked, db, fixed_today):
    """Today 2026-09-26. Retirement (linked, needs sign-in) 38,912.77: holdings 35,000;
    HSA (manual, new this month) 4,210.40, no holdings; Investment 1,000 (Apple 600, cash 400)."""
    item = PlaidItem(plaid_item_id="i1", access_token="access-sandbox-SECRET", institution_name="Harbor",
                     kind="investment", status="login_required",
                     last_synced_at=dt.datetime(2026, 9, 22, 14, 3, 0))
    db.add(item)
    db.commit()
    ret = make_account(db, "Retirement", "retirement", 38912.77, item_id=item.id, plaid_type="investment",
                       plaid_subtype="401k", institution_name="Harbor Retirement", mask="9876")
    hsa = make_account(db, "HSA", "hsa", 4210.40, institution_name="Harbor Retirement")
    inv = make_account(db, "Investment", "investment", 1000, institution_name="Summit")
    make_account(db, "Hidden brokerage", "investment", 99999, hidden=True)
    make_account(db, "Checking", "bank", 5000)
    snap(db, ret, "2026-06-15", 35000)
    snap(db, ret, "2026-07-31", 36000)
    snap(db, ret, "2026-08-20", 37000)
    snap(db, ret, "2026-08-31", 99999, estimated=True)  # never used: estimates aren't history
    snap(db, inv, "2026-07-10", 850)
    snap(db, hsa, "2026-09-10", 4000)
    hold(db, ret, "s_tdf", "Harbor Target Retirement 2045 Fund", 20000)
    hold(db, ret, "s_500", "US 500 Index Fund", 10000)
    hold(db, ret, "s_bnd", "Total Bond Market", 5000)
    hold(db, inv, "s_aapl", "Apple Inc.", 600, ticker="AAPL")
    hold(db, inv, "s_usd", "US Dollar", 400, ticker="CUR:USD")
    db.add(AppSetting(key="security_info", value=json.dumps({
        "s_tdf": {"type": "mutual fund", "subtype": "mutual fund", "cash": False},
        "s_aapl": {"type": "equity", "subtype": "common stock", "cash": False},
        "s_usd": {"type": "cash", "subtype": "cash", "cash": True},
    })))
    db.commit()
    return unlocked, item, ret, hsa, inv


def test_investments_overview(portfolio):
    client, item, ret, hsa, inv = portfolio
    out = ok(client.get("/api/investments"))
    assert out["last_month"] == "2026-08"
    assert out["as_of"].endswith("Z")
    assert out["total"] == {
        "worth": 44123.17, "change": 2062.77, "change_pct": 5.4, "change_missing": ["HSA"],
        "tracked_since": "2026-06", "change_since_tracking": 4273.17,
    }
    assert [a["name"] for a in out["accounts"]] == ["Retirement", "HSA", "Investment"]  # largest first
    r, h, i = out["accounts"]
    assert {k: r[k] for k in ("id", "institution_name", "category", "source", "worth", "change", "change_pct",
                               "tracked_since", "change_since_tracking", "holdings_known")} == {
        "id": ret.id, "institution_name": "Harbor Retirement", "category": "retirement", "source": "plaid",
        "worth": 38912.77, "change": 1912.77, "change_pct": 5.2, "tracked_since": "2026-06",
        "change_since_tracking": 3912.77, "holdings_known": True,
    }
    assert r["connection"] == {"item_id": item.id, "status": "login_required", "kind": "investment",
                               "last_synced_at": "2026-09-22T14:03:00Z"}
    assert "access" not in json.dumps(out) and "9876" not in json.dumps(out)
    assert r["mix"] == [
        {"kind": "target_date", "year": 2045, "value": 20000.0, "pct": 51.4, "names": ["Harbor Target Retirement 2045 Fund"]},
        {"kind": "us_stock", "year": None, "value": 10000.0, "pct": 25.7, "names": ["US 500 Index Fund"]},
        {"kind": "bond", "year": None, "value": 5000.0, "pct": 12.8, "names": ["Total Bond Market"]},
        {"kind": "not_broken_down", "year": None, "value": 3912.77, "pct": 10.1, "names": []},
    ]
    # New this month: no change yet; the bank doesn't say what it holds.
    assert (h["change"], h["change_pct"], h["tracked_since"], h["change_since_tracking"]) == (None, None, "2026-09", 210.4)
    assert (h["source"], h["connection"], h["holdings_known"]) == ("manual", None, False)
    assert h["mix"] == [{"kind": "not_broken_down", "year": None, "value": 4210.4, "pct": 100.0, "names": []}]
    assert (i["change"], i["change_pct"]) == (150.0, 17.6)
    assert i["mix"] == [
        {"kind": "company_stock", "year": None, "value": 600.0, "pct": 60.0, "names": ["Apple Inc."]},
        {"kind": "cash", "year": None, "value": 400.0, "pct": 40.0, "names": ["US Dollar"]},
    ]
    assert [(m["kind"], m["value"]) for m in out["mix"]] == [
        ("target_date", 20000.0), ("us_stock", 10000.0), ("not_broken_down", 8123.17), ("bond", 5000.0),
        ("company_stock", 600.0), ("cash", 400.0),
    ]
    assert out["mix"][0]["pct"] == 45.3


def test_history_carries_forward_and_marks_added_accounts(portfolio):
    client, item, ret, hsa, inv = portfolio
    history = ok(client.get("/api/investments"))["history"]
    assert history["total"] == [
        {"month": "2026-06", "worth": 35000.0, "carried": False, "added": ["Retirement"]},
        {"month": "2026-07", "worth": 36850.0, "carried": False, "added": ["Investment"]},
        {"month": "2026-08", "worth": 37850.0, "carried": True, "added": []},  # Investment carried from July
        {"month": "2026-09", "worth": 44123.17, "carried": False, "added": ["HSA"]},  # today's worth
    ]
    by = history["by_account"]
    assert [p["worth"] for p in by[str(ret.id)]] == [35000.0, 36000.0, 37000.0, 38912.77]
    assert [(p["month"], p["worth"], p["carried"]) for p in by[str(inv.id)]] == [
        ("2026-07", 850.0, False), ("2026-08", 850.0, True), ("2026-09", 1000.0, False)]
    assert by[str(hsa.id)] == [{"month": "2026-09", "worth": 4210.4, "carried": False, "added": []}]


def test_history_is_at_most_61_months(unlocked, db, fixed_today):
    acct = make_account(db, "Old IRA", "retirement", 5000)
    snap(db, acct, "2019-03-10", 1000)
    snap(db, acct, "2021-08-15", 2000)
    points = ok(unlocked.get("/api/investments"))["history"]["total"]
    assert len(points) == 61
    assert points[0] == {"month": "2021-09", "worth": 2000.0, "carried": True, "added": []}
    assert points[-1] == {"month": "2026-09", "worth": 5000.0, "carried": False, "added": []}


def test_empty_and_unsnapshotted(unlocked, db, fixed_today):
    out = ok(unlocked.get("/api/investments"))
    assert out == {
        "as_of": None, "last_month": "2026-08",
        "total": {"worth": 0.0, "change": None, "change_pct": None, "change_missing": [], "tracked_since": None,
                  "change_since_tracking": None},
        "accounts": [], "mix": [], "holdings_differ": False, "history": {"total": [], "by_account": {}},
    }
    acct = make_account(db, "Brokerage", "investment", 300)
    out = ok(unlocked.get("/api/investments"))
    assert out["total"]["change_missing"] == ["Brokerage"] and out["total"]["change"] is None
    assert out["history"]["total"] == [{"month": "2026-09", "worth": 300.0, "carried": False, "added": ["Brokerage"]}]
    assert out["accounts"][0]["tracked_since"] is None


def test_small_rounding_gap_is_not_a_hidden_part(unlocked, db, fixed_today):
    acct = make_account(db, "Brokerage", "investment", 1000.50)
    hold(db, acct, "x", "Some 500 Index Fund", 1000)
    mix = ok(unlocked.get("/api/investments"))["accounts"][0]["mix"]
    assert [m["kind"] for m in mix] == ["us_stock"]


def test_investments_need_a_session(unlocked):
    anon = TestClient(unlocked.app, base_url=BASE_URL, headers=CSRF)
    assert anon.get("/api/investments").status_code == 401


# ------------------------------------------------------------------ security_info from sync


def test_sync_keeps_security_info(unlocked, db, fixed_today):
    a_item = PlaidItem(plaid_item_id="a", access_token="t1", institution_name="A", kind="investment")
    b_item = PlaidItem(plaid_item_id="b", access_token="t2", institution_name="B", kind="investment")
    db.add_all([a_item, b_item])
    db.commit()
    a = make_account(db, "A 401k", "retirement", 100, item_id=a_item.id, plaid_account_id="pa")
    b = make_account(db, "B IRA", "retirement", 100, item_id=b_item.id, plaid_account_id="pb")

    def apply(item, account, pid, holdings, securities):
        result = sync_service.SyncResult(item_id=item.id, institution_name=item.institution_name)
        sync_service._apply_holdings(db, item, {"holdings": holdings, "securities": securities}, {pid: account}, result)  # noqa: SLF001
        db.commit()

    apply(a_item, a, "pa",
          [{"account_id": "pa", "security_id": "s1", "institution_value": 60, "quantity": 1},
           {"account_id": "pa", "security_id": "s2", "institution_value": 40, "quantity": 1}],
          [{"security_id": "s1", "name": "Fund", "type": "Mutual Fund", "subtype": "mutual fund", "is_cash_equivalent": False},
           {"security_id": "s2", "name": "Cash", "type": "cash", "subtype": None, "is_cash_equivalent": True}])
    apply(b_item, b, "pb",
          [{"account_id": "pb", "security_id": "s3", "institution_value": 100, "quantity": 1}],
          [{"security_id": "s3", "name": "Apple", "ticker_symbol": "AAPL", "type": "equity"}])
    info = load_security_info(db)
    assert info == {
        "s1": {"type": "mutual fund", "subtype": "mutual fund", "cash": False},
        "s2": {"type": "cash", "subtype": None, "cash": True},
        "s3": {"type": "equity", "subtype": None, "cash": False},
    }
    # A's next sync no longer holds s2: it's dropped; B's security stays.
    apply(a_item, a, "pa",
          [{"account_id": "pa", "security_id": "s1", "institution_value": 100, "quantity": 1}],
          [{"security_id": "s1", "name": "Fund", "type": "etf"}])
    assert load_security_info(db) == {
        "s1": {"type": "etf", "subtype": None, "cash": False},
        "s3": {"type": "equity", "subtype": None, "cash": False},
    }


def test_bad_security_info_is_ignored(unlocked, db, fixed_today):
    db.add(AppSetting(key="security_info", value="not json"))
    db.commit()
    acct = make_account(db, "Brokerage", "investment", 100)
    hold(db, acct, "x", "Apple Inc.", 100)
    assert ok(unlocked.get("/api/investments"))["accounts"][0]["mix"][0]["kind"] == "other"


def test_holdings_above_a_stale_balance_are_flagged(unlocked, db, fixed_today):
    """L8: when the bank's holdings add up to more than the balance, the mix says so
    (``holdings_differ``) instead of adding up to more than the account is worth."""
    stale = make_account(db, "401k", "retirement", 1000)
    fine = make_account(db, "IRA", "retirement", 500)
    hold(db, stale, "s1", "Fidelity 500 Index", 1200, ticker="FXAIX")
    hold(db, fine, "s2", "Fidelity 500 Index", 499.50, ticker="FXAIX")  # under $1: rounding
    out = ok(unlocked.get("/api/investments"))
    by = {a["name"]: a for a in out["accounts"]}
    assert (by["401k"]["holdings_differ"], by["IRA"]["holdings_differ"], out["holdings_differ"]) == (True, False, True)
    assert by["401k"]["mix"] == [
        {"kind": "us_stock", "year": None, "value": 1200.0, "pct": 100.0, "names": ["Fidelity 500 Index"]},
    ]
    assert sum(m["pct"] for m in out["mix"]) <= 100.05
