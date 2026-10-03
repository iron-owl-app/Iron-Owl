"""The only module that talks to Plaid. Everything returns plain dicts.

Routes get the active client from ``get_plaid_client``: the ``.env`` keys when set,
otherwise the keys saved in the vault (Release 3.4, ``services.plaid_keys``). Tests
replace it via ``app.dependency_overrides[get_plaid_client]`` or ``create_app(plaid_factory=...)``.
Access tokens and the secret are passed in and never logged; Plaid failures are reduced
to ``PlaidError(error_type, error_code, error_message)``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import Request

from .config import Settings

log = logging.getLogger("fintrack.plaid")

KIND_PRODUCTS = {"bank": "transactions", "investment": "investments", "loan": "liabilities"}
ALL_PRODUCTS = tuple(KIND_PRODUCTS.values())
CLIENT_NAME = "Iron Owl"
# Plaid requires a stable per-user id; this is a single-user local app.
CLIENT_USER_ID = "fintrack-local-user"
# Checking keys (Settings -> Bank connection) must not hang the request: (connect, read) seconds.
CHECK_TIMEOUT = (5, 20)
# Access tokens look like ``access-<env>-<uuid>``: the env they were issued in.
_TOKEN_ENV_RE = re.compile(r"^access-(sandbox|production|development)-")
# /item/get ITEM_ERROR codes that prove Plaid found the Item under the calling keys.
_OWNED_ITEM_ERRORS = frozenset({
    "ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT", "ITEM_LOCKED",
    "INVALID_UPDATED_USERNAME", "INVALID_CREDENTIALS", "INSUFFICIENT_CREDENTIALS",
    "USER_SETUP_REQUIRED", "MFA_NOT_SUPPORTED", "NO_ACCOUNTS", "ITEM_NOT_SUPPORTED",
    "ACCESS_NOT_GRANTED",
})


class PlaidError(Exception):
    def __init__(self, error_type: str, error_code: str, error_message: str) -> None:
        super().__init__(f"{error_type}/{error_code}")
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = error_message

    @classmethod
    def from_api_exception(cls, exc: Any) -> PlaidError:
        body: dict[str, Any] = {}
        try:
            parsed = json.loads(exc.body or "{}")
            if isinstance(parsed, dict):
                body = parsed
        except (TypeError, ValueError):
            pass
        return cls(
            str(body.get("error_type") or "API_ERROR"),
            str(body.get("error_code") or f"HTTP_{getattr(exc, 'status', 'ERROR')}"),
            str(body.get("error_message") or "Plaid request failed"),
        )


class PlaidNotConfigured(Exception):
    pass


class PlaidClient:
    """A client for one set of keys. Never mutated after construction: changing keys
    means building a new client (``from_keys``), so in-flight calls stay consistent."""

    def __init__(self, settings: Settings) -> None:
        configured = settings.plaid_configured
        secret = settings.plaid_secret.get_secret_value() if configured else ""
        self._setup(settings.plaid_client_id, secret, settings.plaid_env, "env" if configured else "none")

    @classmethod
    def from_keys(cls, client_id: str, secret: str, env: str) -> PlaidClient:
        """A client for keys saved in the vault (or candidate keys being checked)."""
        client = cls.__new__(cls)
        client._setup(client_id, secret, env, "vault")
        return client

    def _setup(self, client_id: str, secret: str, env: str, source: str) -> None:
        self.env = env
        self.source = source
        self.configured = bool(client_id and secret)
        self._api = None
        if self.configured:
            import plaid
            from plaid.api import plaid_api

            host = plaid.Environment.Production if env == "production" else plaid.Environment.Sandbox
            # Never set ``configuration.debug``: http.client would print the request
            # headers, PLAID-SECRET included, to stdout.
            configuration = plaid.Configuration(
                host=host,
                api_key={"clientId": client_id, "secret": secret, "plaidVersion": "2020-09-14"},
            )
            self._api = plaid_api.PlaidApi(plaid.ApiClient(configuration))

    def __repr__(self) -> str:  # never include the keys
        return f"<PlaidClient env={self.env} configured={self.configured} source={self.source}>"

    # ------------------------------------------------------------------ plumbing

    def _call(self, method: str, request: Any, *, timeout: Any = None) -> dict[str, Any]:
        if self._api is None:
            raise PlaidNotConfigured()
        import plaid

        try:
            if timeout is None:
                response = getattr(self._api, method)(request)
            else:
                response = getattr(self._api, method)(request, _request_timeout=timeout)
        except plaid.ApiException as exc:
            err = PlaidError.from_api_exception(exc)
            log.warning("plaid %s failed: %s/%s", method, err.error_type, err.error_code)
            raise err from None
        except Exception as exc:  # network errors etc.; never include request data
            log.warning("plaid %s failed: %s", method, type(exc).__name__)
            raise PlaidError("API_ERROR", "NETWORK_ERROR", "Could not reach Plaid") from None
        return response.to_dict()

    # ------------------------------------------------------------------ endpoints

    def check_keys(self) -> None:
        """Prove the client ID + secret work in this env; raises ``PlaidError`` if not.

        /institutions/get is read-only, free and doesn't depend on product approval
        (unlike /link/token/create), so a failure here is about the keys.
        """
        from plaid.model.country_code import CountryCode
        from plaid.model.institutions_get_request import InstitutionsGetRequest

        self._call(
            "institutions_get",
            InstitutionsGetRequest(count=1, offset=0, country_codes=[CountryCode("US")]),
            timeout=CHECK_TIMEOUT,
        )

    def owns_item(self, access_token: str) -> bool:
        """Whether these keys can reach an Item (it was linked with the same Plaid account).

        Conservative: owned only on success, or on an ``ITEM_ERROR`` in ``_OWNED_ITEM_ERRORS``
        (e.g. ITEM_LOGIN_REQUIRED), which Plaid only returns for an Item it found under these
        keys. Any other ``ITEM_ERROR`` (notably ITEM_NOT_FOUND) and ``INVALID_INPUT``
        (INVALID_ACCESS_TOKEN) mean it isn't. Anything else is re-raised. /item/get is free.
        """
        from plaid.model.item_get_request import ItemGetRequest

        try:
            self._call("item_get", ItemGetRequest(access_token=access_token), timeout=CHECK_TIMEOUT)
        except PlaidError as err:
            if err.error_type == "ITEM_ERROR":
                return err.error_code in _OWNED_ITEM_ERRORS
            if err.error_type == "INVALID_INPUT":
                return False
            raise
        return True

    def create_link_token(self, kind: str, access_token: str | None = None) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.link_token_transactions import LinkTokenTransactions
        from plaid.model.products import Products

        kwargs: dict[str, Any] = {
            "client_name": CLIENT_NAME,
            "language": "en",
            "country_codes": [CountryCode("US")],
            "user": LinkTokenCreateRequestUser(client_user_id=CLIENT_USER_ID),
        }
        if access_token:
            kwargs["access_token"] = access_token  # update mode: no products
        else:
            # `kind` picks the product the institution must support; the others ride along as
            # optional so one link imports every account at the institution. Linking the same
            # bank once per product would create separate Items that duplicate its accounts.
            required = KIND_PRODUCTS[kind]
            kwargs["products"] = [Products(required)]
            kwargs["optional_products"] = [Products(p) for p in ALL_PRODUCTS if p != required]
            kwargs["transactions"] = LinkTokenTransactions(days_requested=730)
        return self._call("link_token_create", LinkTokenCreateRequest(**kwargs))["link_token"]

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

        data = self._call(
            "item_public_token_exchange", ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        return data["access_token"], data["item_id"]

    def get_institution(self, access_token: str) -> tuple[str | None, str | None]:
        """(institution_id, institution_name) for an item; best effort."""
        from plaid.model.country_code import CountryCode
        from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
        from plaid.model.item_get_request import ItemGetRequest

        item = self._call("item_get", ItemGetRequest(access_token=access_token)).get("item") or {}
        institution_id = item.get("institution_id")
        name = item.get("institution_name")
        if institution_id and not name:
            try:
                inst = self._call(
                    "institutions_get_by_id",
                    InstitutionsGetByIdRequest(
                        institution_id=institution_id, country_codes=[CountryCode("US")]
                    ),
                )
                name = (inst.get("institution") or {}).get("name")
            except PlaidError:
                pass
        return institution_id, name

    def item_products(self, access_token: str) -> set[str]:
        """Products actually enabled (and billed) on the Item, per /item/get.

        Sync only calls endpoints for these: calling e.g. /transactions/sync on an Item
        without Transactions makes Plaid add (and bill) the product.
        """
        from plaid.model.item_get_request import ItemGetRequest

        item = self._call("item_get", ItemGetRequest(access_token=access_token)).get("item") or {}
        return {str(getattr(p, "value", p)) for p in item.get("products") or []}

    def get_accounts(self, access_token: str) -> list[dict[str, Any]]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        return self._call("accounts_get", AccountsGetRequest(access_token=access_token))["accounts"]

    def sync_transactions(self, access_token: str, cursor: str | None) -> dict[str, Any]:
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        kwargs: dict[str, Any] = {"access_token": access_token, "count": 500}
        if cursor:
            kwargs["cursor"] = cursor
        return self._call("transactions_sync", TransactionsSyncRequest(**kwargs))

    def get_holdings(self, access_token: str) -> dict[str, Any]:
        from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

        return self._call(
            "investments_holdings_get", InvestmentsHoldingsGetRequest(access_token=access_token)
        )

    def get_liabilities(self, access_token: str) -> dict[str, Any]:
        from plaid.model.liabilities_get_request import LiabilitiesGetRequest

        return self._call("liabilities_get", LiabilitiesGetRequest(access_token=access_token))

    def remove_item(self, access_token: str) -> None:
        from plaid.model.item_remove_request import ItemRemoveRequest

        self._call("item_remove", ItemRemoveRequest(access_token=access_token))


def token_env(access_token: str) -> str | None:
    """The Plaid env an access token was issued in, or None if it doesn't say."""
    match = _TOKEN_ENV_RE.match(access_token or "")
    return match.group(1) if match else None


def get_plaid_client(request: Request) -> PlaidClient:
    return request.app.state.fintrack.active_plaid()
