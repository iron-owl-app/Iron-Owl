"""Plaid keys saved in the vault (SPEC Release 3.4, Settings -> Bank connection).

Three ``app_settings`` rows, inside the SQLCipher database, so they are encrypted at rest
and travel (encrypted) with every backup:

- ``plaid_keys_meta``: non-secret JSON (client ID, env, last 4 of the secret, when saved).
  Status reads only this row, so a GET never loads the secret.
- ``plaid_keys_secret``: the secret.
- ``plaid_keys_last_test``: the last check of the saved keys.

Keys in ``.env`` win (``AppState.active_plaid``); then the vault's keys are neither loaded
nor used. The secret is never returned, logged or put in an exception message. Checks call
Plaid through a throwaway client from ``state.plaid_factory``, with no DB transaction open;
nothing changes until a save commits.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import security
from ..models import PlaidItem
from ..plaid_client import PlaidError, token_env
from ..utils import iso_utc, utcnow
from .automation import _get, _put

log = logging.getLogger("fintrack.plaid_keys")

META_KEY = "plaid_keys_meta"
SECRET_KEY = "plaid_keys_secret"
LAST_TEST_KEY = "plaid_keys_last_test"
ROW_KEYS = (META_KEY, SECRET_KEY, LAST_TEST_KEY)

ENVS = ("sandbox", "production")
# env "auto" (candidate keys only): production first, so approved keys aren't mistaken
# for Sandbox ones; the secrets differ per env, so at most one of them works.
AUTO_ORDER = ("production", "sandbox")
CODES = ("ok", "invalid_keys", "wrong_environment", "items_mismatch", "unreachable", "plaid_error")
# When no env accepted the keys, the most actionable failure wins.
AUTO_PRIORITY = ("unreachable", "plaid_error", "items_mismatch", "wrong_environment", "invalid_keys")
# PUT's status for a failed check (nothing is stored).
SAVE_STATUS = {
    "invalid_keys": 422, "wrong_environment": 422, "items_mismatch": 409,
    "unreachable": 502, "plaid_error": 502,
}

_KEY_RE = re.compile(r"[A-Za-z0-9]{16,64}")
_PLAID_CODE_RE = re.compile(r"[A-Z0-9_]{1,64}")
# Shorter .env secrets get no hint: the last 4 would be too much of them.
MIN_HINT_SOURCE = 16

ENV_NAMES = {"sandbox": "Sandbox", "production": "Production"}
ENV_LABELS = {"production": "Real banks (Production)", "sandbox": "Test banks (Sandbox)"}

NOT_SET = "No Plaid keys are set yet."
THROTTLED = "Wait a moment before testing again."
MANAGED_BY_ENV = (
    "Your Plaid keys are set in Iron Owl’s settings file (.env), so they’re managed there. "
    "To manage them here instead, remove PLAID_CLIENT_ID and PLAID_SECRET from that file "
    "and restart Iron Owl."
)
SAVE_PASSWORD_MISSING = "Enter your current Iron Owl password to change your Plaid keys."
REMOVE_PASSWORD_MISSING = "Enter your current Iron Owl password to remove your Plaid keys."
ITEMS_CHANGED = "Your linked banks changed while Iron Owl was checking these keys. Please try again."


def message(code: str, env: str | None, linked: int = 0) -> str:
    """Plain-language result text (server constants; Plaid’s own text is never passed on).

    ``env`` is None for an ``"auto"`` check that no env accepted.
    """
    if code == "ok":
        return f"These keys work — {ENV_LABELS[env]}." if env in ENV_LABELS else "These keys work."
    if code == "invalid_keys":
        if env not in ENV_NAMES:
            return (
                "Plaid didn’t accept this Client ID and Secret. "
                "Check that you copied both from Developers → Keys."
            )
        return (
            "Plaid didn’t accept this Client ID and Secret. Check that you copied both from "
            f"Developers → Keys, and that you copied the {ENV_NAMES[env]} secret "
            "(Sandbox and Production secrets are different)."
        )
    if code == "wrong_environment":
        return (
            "These keys aren’t approved for real banks yet. Finish Plaid’s Production approval "
            "first, or use your Sandbox secret to try Iron Owl with test banks."
        )
    if code == "items_mismatch":
        if linked == 1:
            banks, were, them = "your linked institution", "It was", "it"
        else:
            banks, were, them = f"your {linked} linked institutions", "They were", "them"
        return (
            f"These keys can’t reach {banks}. {were} linked with a different Plaid account "
            f"or a different kind of secret. Use the keys you linked {them} with, or remove "
            f"{them} under Linked institutions first."
        )
    if code == "unreachable":
        return "Couldn’t reach Plaid. Check your internet connection and try again."
    return "Plaid had a problem checking these keys. Wait a minute and try again."


# ------------------------------------------------------------------ checking keys


def _now_iso() -> str:
    return iso_utc(utcnow()) or ""


@dataclass(frozen=True)
class KeyCheck:
    code: str
    # The env that worked, or the one tried by a single-env check; None when an "auto"
    # check found no env that works.
    env: str | None
    plaid_code: str | None = None
    checked_linked_items: bool = False
    linked: int = 0
    at: str = field(default_factory=_now_iso)

    @property
    def ok(self) -> bool:
        return self.code == "ok"

    def result(self) -> dict:
        """The ``PlaidKeysTest`` body."""
        return {
            "ok": self.ok,
            "code": self.code,
            "message": message(self.code, self.env, self.linked),
            "plaid_code": self.plaid_code,
            "at": self.at,
            "env": self.env,
            "checked_linked_items": self.checked_linked_items,
        }


def classify(err: PlaidError) -> str:
    """Result code for a failed /institutions/get."""
    if err.error_code == "INVALID_API_KEYS":
        return "invalid_keys"
    if err.error_code == "UNAUTHORIZED_ENVIRONMENT":
        return "wrong_environment"
    if err.error_code == "NETWORK_ERROR":
        return "unreachable"
    if err.error_type == "INVALID_INPUT":
        return "invalid_keys"
    return "plaid_error"


def sanitized_code(err: PlaidError) -> str:
    code = err.error_code if isinstance(err.error_code, str) else ""
    return code if _PLAID_CODE_RE.fullmatch(code) else "UNKNOWN"


Factory = Callable[[str, str, str], Any]


def check(
    factory: Factory, client_id: str, secret: str, env: str, tokens: Sequence[str], *, probe_items: bool = True
) -> KeyCheck:
    """Do these keys work in ``env``, and do they own the linked Items (SPEC "Checking keys")?

    ``tokens`` are every Item's access token (pending included), ordered by id. Call with
    no DB transaction open: this makes up to two Plaid calls. ``probe_items=False`` skips
    the ownership probe (only the local token-env check runs), so an access token is
    never sent along with unconfirmed candidate keys; the password-gated save probes.
    """
    linked = len(tokens)
    # Local check first: a token says which env it was issued in (e.g. sandbox -> production).
    if any((token_env(t) or env) != env for t in tokens):
        return KeyCheck("items_mismatch", env, linked=linked)
    try:
        client = factory(client_id, secret, env)
    except Exception as exc:  # noqa: BLE001 - never include the exception text
        log.warning("could not build a Plaid client: %s", type(exc).__name__)
        return KeyCheck("plaid_error", env, plaid_code="UNKNOWN", linked=linked)
    try:
        client.check_keys()
    except PlaidError as err:
        return KeyCheck(classify(err), env, plaid_code=sanitized_code(err), linked=linked)
    if not tokens:
        return KeyCheck("ok", env)
    if not probe_items:
        return KeyCheck("ok", env, linked=linked)
    # Ownership probe: new keys must reach the Items already linked, or they could no
    # longer be synced or removed at Plaid (and would stay billed at the old account).
    try:
        owned = client.owns_item(tokens[0])
    except PlaidError as err:
        code = "unreachable" if err.error_code == "NETWORK_ERROR" else "plaid_error"
        return KeyCheck(code, env, plaid_code=sanitized_code(err), checked_linked_items=True, linked=linked)
    return KeyCheck("ok" if owned else "items_mismatch", env, checked_linked_items=True, linked=linked)


def check_auto(
    factory: Factory, client_id: str, secret: str, tokens: Sequence[str], *, probe_items: bool = True
) -> KeyCheck:
    """``env: "auto"``: find the env these keys belong to (UX addendum U2).

    With linked Items only the env their tokens were issued in is tried; otherwise
    production, then sandbox, and the first that works wins. A network failure stops at
    once (the other env is behind the same connection). ``probe_items`` as in ``check``.
    """
    implied = {token_env(t) for t in tokens} - {None}
    if implied:
        if len(implied) > 1 or not implied <= set(ENVS):
            return KeyCheck("items_mismatch", None, linked=len(tokens))
        result = check(factory, client_id, secret, implied.pop(), tokens, probe_items=probe_items)
        return result if result.ok else dataclasses.replace(result, env=None)
    failures: list[KeyCheck] = []
    for env in AUTO_ORDER:
        result = check(factory, client_id, secret, env, tokens, probe_items=probe_items)
        if result.ok:
            return result
        failures.append(result)
        if result.code == "unreachable":
            break
    worst = min(failures, key=lambda r: AUTO_PRIORITY.index(r.code))
    return dataclasses.replace(worst, env=None)


class CheckThrottle:
    """``POST /test``: one check at a time, and at least 2s between starts.

    Stops an open session from hammering Plaid; separate from the password limiter.
    """

    MIN_INTERVAL = 2.0

    def __init__(self) -> None:
        self._busy = threading.Lock()
        self._last_start: float | None = None

    def start(self) -> int | None:
        """Claim the slot: None, or the seconds to wait (429)."""
        if not self._busy.acquire(blocking=False):
            return math.ceil(self.MIN_INTERVAL)
        t = security.now()
        if self._last_start is not None and t - self._last_start < self.MIN_INTERVAL:
            self._busy.release()
            return max(1, math.ceil(self.MIN_INTERVAL - (t - self._last_start)))
        self._last_start = t
        return None

    def done(self) -> None:
        self._busy.release()


# ------------------------------------------------------------------ storage


@dataclass(frozen=True)
class Keys:
    client_id: str
    secret: str = field(repr=False)
    env: str


def _warn_unreadable(what: str, exc: Exception) -> None:
    log.warning("ignoring unreadable saved Plaid %s: %s", what, type(exc).__name__)


def read_meta(session: Session) -> dict | None:
    raw = _get(session, META_KEY)
    if raw is None:
        return None
    try:
        meta = json.loads(raw)
        if not (
            isinstance(meta, dict)
            and meta.get("v") == 1
            and isinstance(meta.get("client_id"), str)
            and _KEY_RE.fullmatch(meta["client_id"])
            and meta.get("env") in ENVS
            and isinstance(meta.get("secret_hint"), str)
            and isinstance(meta.get("saved_at"), str)
        ):
            raise ValueError("unexpected shape")
    except (TypeError, ValueError) as exc:
        _warn_unreadable("keys", exc)
        return None
    return meta


def read_keys(session: Session) -> Keys | None:
    meta = read_meta(session)
    secret = _get(session, SECRET_KEY)
    if meta is None or not secret:
        return None
    return Keys(meta["client_id"], secret, meta["env"])


def read_last_test(session: Session) -> dict | None:
    raw = _get(session, LAST_TEST_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not (
            isinstance(data, dict)
            and data.get("code") in CODES
            and isinstance(data.get("ok"), bool)
            and isinstance(data.get("message"), str)
            and isinstance(data.get("at"), str)
            and (data.get("plaid_code") is None or isinstance(data.get("plaid_code"), str))
            and data.get("env") in (*ENVS, None)
        ):
            raise ValueError("unexpected shape")
    except (TypeError, ValueError) as exc:
        _warn_unreadable("key check", exc)
        return None
    return {k: data.get(k) for k in ("ok", "code", "message", "plaid_code", "at", "env")}


def write_last_test(session: Session, result: dict) -> None:
    stored = {k: result.get(k) for k in ("ok", "code", "message", "plaid_code", "at", "env")}
    _put(session, LAST_TEST_KEY, json.dumps(stored))


def write_keys(session: Session, client_id: str, secret: str, env: str, last_test: dict) -> None:
    """Meta, secret and the passing check, in the caller's transaction."""
    meta = {
        "v": 1,
        "client_id": client_id,
        "env": env,
        "secret_hint": secret[-4:],
        "saved_at": _now_iso(),
    }
    _put(session, META_KEY, json.dumps(meta))
    _put(session, SECRET_KEY, secret)
    write_last_test(session, last_test)


def delete_keys(session: Session) -> None:
    for key in ROW_KEYS:
        _put(session, key, None)


def has_rows(session: Session) -> bool:
    return any(_get(session, key) is not None for key in ROW_KEYS)


def item_tokens(session: Session) -> list[str]:
    """Every Item's access token (pending included), oldest first."""
    return list(session.scalars(select(PlaidItem.access_token).order_by(PlaidItem.id)))


def status(session: Session, state: Any) -> dict:
    """``PlaidKeysStatus``. Never reads the vault's secret."""
    settings = state.settings
    env_id = settings.plaid_client_id
    env_secret = settings.plaid_secret.get_secret_value()
    linked = session.scalar(select(func.count()).select_from(PlaidItem)) or 0
    out: dict[str, Any] = {
        "source": "none",
        "client_id": None,
        "env": None,
        "secret_hint": None,
        "saved_at": None,
        "last_test": None,
        "vault_keys_ignored": False,
        "env_file_partial": bool(env_id) != bool(env_secret),
        "linked_items": linked,
    }
    if settings.plaid_configured:
        out.update(
            source="env",
            client_id=env_id,
            env=settings.plaid_env,
            secret_hint=env_secret[-4:] if len(env_secret) >= MIN_HINT_SOURCE else None,
            vault_keys_ignored=has_rows(session),
        )
        return out
    meta = read_meta(session)
    if meta is not None and _get(session, SECRET_KEY):
        out.update(
            source="vault",
            client_id=meta["client_id"],
            env=meta["env"],
            secret_hint=meta["secret_hint"],
            saved_at=meta["saved_at"],
            last_test=read_last_test(session),
        )
    return out


# ------------------------------------------------------------------ the vault's client


class PlaidKeyStore:
    """Holds the client built from the vault's keys, only while unlocked.

    Clients are immutable and swapped by reference, so in-flight requests and a running
    auto-sync keep the client they captured. ``reload`` rebuilds from the committed rows
    (after unlock, restore, save and remove); ``clear`` runs after every lock, so the
    secret isn't held once the vault is locked. A Python ``str`` can't be wiped: dropping
    the reference is best effort, like ``db._wipe``.
    """

    def __init__(self, factory: Factory) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self.client: Any = None
        self.throttle = CheckThrottle()

    def reload(self, state: Any) -> None:
        """Never raises: a failure leaves no vault client (the app then acts unconfigured).

        The new client is built first and then swapped in, so a request never sees a
        transient ``None`` while the keys are merely reloaded.
        """
        with self._lock:
            if state.plaid.configured:
                self.client = None
                return  # .env keys win: don't hold the vault's secret in memory
            vault = state.vault
            try:
                session = vault.db.acquire()
            except Exception:  # noqa: BLE001 - locked (DatabaseLocked) or locking
                self.client = None
                return
            try:
                keys = read_keys(session)
                session.commit()
            except Exception as exc:  # noqa: BLE001
                log.error("could not read the saved Plaid keys: %s", type(exc).__name__)
                self.client = None
                return
            finally:
                vault.db.release(session)
            if keys is None:
                self.client = None
                return
            try:
                client = self._factory(keys.client_id, keys.secret, keys.env)
            except Exception as exc:  # noqa: BLE001 - never include the exception text
                log.error("could not load the saved Plaid keys: %s", type(exc).__name__)
                client = None
            self.client = client

    def clear(self) -> None:
        with self._lock:
            self.client = None
