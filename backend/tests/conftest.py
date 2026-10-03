from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.plaid_client import PlaidError, get_plaid_client
from app.security import SESSION_HEADER

PASSWORD = "correct horse battery staple"
BASE_URL = "http://127.0.0.1:8000"
CSRF = {"X-FinTrack": "1"}


SESSION_ROUTES = (
    "/api/auth/setup", "/api/auth/unlock", "/api/auth/change-password", "/api/restore", "/api/auth/recover",
)


class SessionClient(TestClient):
    """TestClient that keeps the session token in a header, like the frontend does."""

    def request(self, method, url, *args, **kwargs):
        response = super().request(method, url, *args, **kwargs)
        if response.status_code == 200 and str(url).endswith(SESSION_ROUTES):
            self.headers[SESSION_HEADER] = response.json()["session_token"]
        return response


def make_settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "plaid_client_id": "",
        "plaid_secret": "",
        "plaid_env": "sandbox",
        "auto_lock_minutes": 15,
        "host": "127.0.0.1",
        "port": 8000,
        "data_dir": tmp_path / "data",
        "frontend_dist": tmp_path / "dist",
        "debug": False,
        "dev": False,
    }
    values.update(overrides)
    return Settings(**values)


class FakePlaid:
    """In-memory stand-in for PlaidClient. Scripted per access token."""

    configured = True
    env = "sandbox"

    def __init__(self) -> None:
        self.public_tokens: dict[str, tuple[str, str, str, str]] = {}
        self.accounts: dict[str, list[dict]] = {}
        # access_token -> list of pages; each page is served for the cursor in "cursor_in"
        self.txn_pages: dict[str, list[dict]] = {}
        self.holdings: dict[str, dict] = {}
        self.liabilities: dict[str, dict] = {}
        # (access_token, method) -> list of errors to raise, consumed in order
        self.errors: dict[tuple[str, str], list[PlaidError]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.removed: list[str] = []
        self.products: dict[str, set[str]] = {}  # access_token -> /item/get products

    def add_item(
        self, public_token: str, access_token: str, item_id: str, inst: str, inst_id: str | None = None
    ) -> None:
        self.public_tokens[public_token] = (access_token, item_id, inst_id or f"ins_{item_id}", inst)

    def fail(self, access_token: str, method: str, code: str, error_type: str = "ITEM_ERROR") -> None:
        self.errors.setdefault((access_token, method), []).append(
            PlaidError(error_type, code, f"fake {code}")
        )

    def _maybe_fail(self, access_token: str, method: str) -> None:
        errs = self.errors.get((access_token, method))
        if errs:
            raise errs.pop(0)

    # --- PlaidClient interface

    def create_link_token(self, kind: str, access_token: str | None = None) -> str:
        self.calls.append(("link", kind, access_token))
        return f"link-sandbox-{kind}-{'update' if access_token else 'new'}"

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        access_token, item_id, _, _ = self.public_tokens[public_token]
        return access_token, item_id

    def get_institution(self, access_token: str) -> tuple[str | None, str | None]:
        for tok, _item_id, inst_id, name in self.public_tokens.values():
            if tok == access_token:
                return inst_id, name
        return None, None

    def item_products(self, access_token: str) -> set[str]:
        self.calls.append(("item_products", access_token, None))
        return set(self.products.get(access_token, set()))

    def get_accounts(self, access_token: str) -> list[dict]:
        self.calls.append(("accounts", access_token, None))
        self._maybe_fail(access_token, "accounts")
        return copy.deepcopy(self.accounts.get(access_token, []))

    def sync_transactions(self, access_token: str, cursor: str | None) -> dict:
        self.calls.append(("transactions", access_token, cursor))
        self._maybe_fail(access_token, "transactions")
        for page in self.txn_pages.get(access_token, []):
            if page["cursor_in"] == cursor:
                return copy.deepcopy({k: v for k, v in page.items() if k != "cursor_in"})
        return {"added": [], "modified": [], "removed": [], "next_cursor": cursor or "c0", "has_more": False}

    def get_holdings(self, access_token: str) -> dict:
        self.calls.append(("holdings", access_token, None))
        self._maybe_fail(access_token, "holdings")
        return copy.deepcopy(self.holdings.get(access_token, {"accounts": [], "holdings": [], "securities": []}))

    def get_liabilities(self, access_token: str) -> dict:
        self.calls.append(("liabilities", access_token, None))
        self._maybe_fail(access_token, "liabilities")
        return copy.deepcopy(self.liabilities.get(access_token, {"accounts": [], "liabilities": {}}))

    def remove_item(self, access_token: str) -> None:
        self._maybe_fail(access_token, "remove")
        self.removed.append(access_token)


class FakeKeyPlaid(FakePlaid):
    """A client built by ``FakeKeyFactory`` from keys (saved in the vault, or candidates).

    ``check_keys`` is scripted by the secret's prefix (see ``FakeKeyFactory.outcome``);
    ``owns_item`` by ``factory.owned``. Its errors carry the secret in ``error_message``,
    so tests can prove Plaid's free text is never passed on or logged.
    """

    def __init__(self, factory: FakeKeyFactory, secret: str, env: str) -> None:
        super().__init__()
        self.env = env
        self.source = "vault"
        self._factory = factory
        self._error = factory.outcome(secret, env)
        self._leak = secret  # only ever put in error_message, like a hostile Plaid would

    def __repr__(self) -> str:
        return f"<FakeKeyPlaid env={self.env}>"

    def check_keys(self) -> None:
        self._factory.checks.append(self.env)
        if self._error is not None:
            error_type, code = self._error
            raise PlaidError(error_type, code, f"fake {code} for {self._leak}")

    def owns_item(self, access_token: str) -> bool:
        self._factory.probes.append(access_token)
        if self._factory.probe_error is not None:
            raise self._factory.probe_error
        return access_token in self._factory.owned


class FakeKeyFactory:
    """Stand-in for ``PlaidClient.from_keys`` (``create_app(plaid_factory=...)``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.clients: list[FakeKeyPlaid] = []
        self.checks: list[str] = []  # env of every check_keys call
        self.probes: list[str] = []  # token of every owns_item call
        self.owned: set[str] = set()
        self.probe_error: PlaidError | None = None
        self.forced: dict[str, tuple[str, str]] = {}  # env -> (error_type, error_code)

    def __call__(self, client_id: str, secret: str, env: str) -> FakeKeyPlaid:
        self.calls.append((client_id, secret, env))
        client = FakeKeyPlaid(self, secret, env)
        self.clients.append(client)
        return client

    def outcome(self, secret: str, env: str) -> tuple[str, str] | None:
        if env in self.forced:
            return self.forced[env]
        invalid = ("INVALID_INPUT", "INVALID_API_KEYS")
        if secret.startswith("bad"):
            return invalid
        if secret.startswith("net"):
            return ("API_ERROR", "NETWORK_ERROR")
        if secret.startswith("unauth"):  # Production keys not approved yet
            return ("INVALID_INPUT", "UNAUTHORIZED_ENVIRONMENT") if env == "production" else invalid
        if secret.startswith("weird"):
            return ("API_ERROR", "x y<script>")
        if secret.startswith("rate"):
            return ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_EXCEEDED")
        if secret.startswith("sbx") and env != "sandbox":
            return invalid
        if secret.startswith("prd") and env != "production":
            return invalid
        return None


@pytest.fixture
def key_factory() -> FakeKeyFactory:
    return FakeKeyFactory()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def fake_plaid() -> FakePlaid:
    return FakePlaid()


@pytest.fixture
def app(settings, fake_plaid):
    application = create_app(settings)
    application.dependency_overrides[get_plaid_client] = lambda: fake_plaid
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        yield c


@pytest.fixture
def unlocked(client) -> TestClient:
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def vault(app):
    return app.state.fintrack.vault


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr("app.security.now", c)
    return c


# ------------------------------------------------------------------ Release 2 helpers

import datetime as dt  # noqa: E402
import sys  # noqa: E402

from app import utils as app_utils  # noqa: E402

FIXED_TODAY = dt.date(2026, 9, 26)  # day 26 of 30: pace 26/30, 4 days left


class Today:
    def __init__(self, day: dt.date) -> None:
        self.day = day

    def __call__(self) -> dt.date:
        return self.day

    def set(self, day: dt.date) -> None:
        self.day = day


@pytest.fixture
def fixed_today(monkeypatch) -> Today:
    """Pin "today" everywhere (routers/services bind ``utils.today`` at import time)."""
    clock = Today(FIXED_TODAY)
    real = app_utils.today
    targets = [
        module for name, module in list(sys.modules.items())
        if (name == "app" or name.startswith("app.")) and getattr(module, "today", None) is real
    ]
    for module in targets:
        monkeypatch.setattr(module, "today", clock)
    return clock


@pytest.fixture
def db(unlocked, vault):
    """A session on the unlocked vault, for arranging data directly."""
    session = vault.db.acquire()
    try:
        yield session
    finally:
        vault.db.release(session)


_txn_seq = [0]


def make_account(session, name="Checking", category="bank", balance=0.0, **kw):
    from app.models import Account

    account = Account(
        source=kw.pop("source", "plaid" if kw.get("plaid_type") else "manual"),
        name=name,
        category=category,
        current_balance_cents=round(balance * 100),
        hidden=kw.pop("hidden", False),
        **kw,
    )
    session.add(account)
    session.commit()
    return account


def add_txn(session, account_id, day, amount, name="Txn", merchant=None, category="FOOD_AND_DRINK",
            pending=False, **kw):
    from app.models import Transaction

    _txn_seq[0] += 1
    txn = Transaction(
        account_id=account_id,
        plaid_transaction_id=kw.pop("plaid_transaction_id", f"t_{_txn_seq[0]}"),
        date=day if isinstance(day, dt.date) else dt.date.fromisoformat(day),
        name=name,
        merchant_name=merchant,
        amount_cents=round(amount * 100),
        plaid_category=category,
        category=kw.pop("effective", category),
        pending=pending,
        category_source=kw.pop("category_source", "plaid"),
        is_transfer=kw.pop("is_transfer", False),
        **kw,
    )
    session.add(txn)
    session.commit()
    return txn


def add_budget(session, month, assigned: dict[str, float], removed=()):
    """Budget history written directly (past months are read-only through the API)."""
    from app.models import Budget

    for category, amount in assigned.items():
        session.add(Budget(month=month, category=category, limit_cents=round(amount * 100)))
    for category in removed:
        session.add(Budget(month=month, category=category, limit_cents=0, removed=True))
    session.commit()


# ------------------------------------------------------------------ Settings (D7): never a real dialog


@pytest.fixture(autouse=True)
def _no_real_folder_dialog(monkeypatch):
    """The native folder picker must never open during tests: a test that forgets to mock
    it fails instead of hanging on a real Windows dialog."""
    from app.services import folder_picker

    def refuse() -> str | None:
        raise AssertionError("a test tried to open the real folder picker")

    monkeypatch.setattr(folder_picker, "native_dialog", refuse)
    # Closing it (on every lock) never posts to real windows in tests.
    monkeypatch.setattr(folder_picker, "close_thread_windows", lambda thread_id: 0)


# ------------------------------------------------------------------ Release 3.17: plans repeat


@pytest.fixture(autouse=True)
def _plans_repeat_pinned(monkeypatch):
    """Setup and unlock set ``budget_plans_repeat_from`` to the real month they run in; tests
    pin it themselves instead (absent = plans don't repeat, the rule before Release 3.17).
    ``tests/test_budget_repeat.py`` checks the real hook through ``REAL_START_PLANS_REPEAT``."""
    from app.services import spending

    monkeypatch.setattr(spending, "safe_start_plans_repeat", lambda session, today: None)


def _real_start_plans_repeat():
    from app.services import spending

    return spending.safe_start_plans_repeat


REAL_START_PLANS_REPEAT = _real_start_plans_repeat()
