"""Settings redesign (D7): app preferences stored in the vault's ``app_settings`` (no migration).

- ``auto_lock_minutes``: 5 | 15 | 30 | 60. Overrides the env value (``Settings.auto_lock_minutes``)
  while the vault is unlocked: loaded into the Vault after every unlock and applied at once by
  ``PATCH /api/settings``; every lock drops it (a locked vault uses the env value).
- ``paycheck_notify``: "Tell me when a paycheck is different" (default on). Off: Budget's
  ``income.event`` is null and the ``income`` alert uses its plain wording.
- ``password_changed_at``: ISO-Z, written by setup, change-password and recover.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.orm import Session

from ..models import AppSetting
from ..utils import iso_utc, utcnow

log = logging.getLogger("fintrack.prefs")

AUTO_LOCK_KEY = "auto_lock_minutes"
PAYCHECK_NOTIFY_KEY = "paycheck_notify"
PASSWORD_CHANGED_KEY = "password_changed_at"
AUTO_LOCK_CHOICES = (5, 15, 30, 60)


def _get(session: Session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row is not None else None


def _put(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def stored_auto_lock(session: Session) -> int | None:
    """The auto-lock minutes saved in the vault, or None (not set, or not a valid choice)."""
    raw = _get(session, AUTO_LOCK_KEY)
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if value in AUTO_LOCK_CHOICES else None


def set_auto_lock(session: Session, minutes: int) -> None:
    if minutes not in AUTO_LOCK_CHOICES:  # the schema already refuses anything else
        raise ValueError("auto_lock_minutes must be one of 5, 15, 30, 60")
    _put(session, AUTO_LOCK_KEY, str(minutes))


def paycheck_notify(session: Session) -> bool:
    return _get(session, PAYCHECK_NOTIFY_KEY) != "0"


def set_paycheck_notify(session: Session, on: bool) -> None:
    _put(session, PAYCHECK_NOTIFY_KEY, "1" if on else "0")


def password_changed_at(session: Session) -> str | None:
    raw = _get(session, PASSWORD_CHANGED_KEY)
    if not raw:
        return None
    try:
        return iso_utc(dt.datetime.fromisoformat(raw.rstrip("Z")))
    except ValueError:
        return None


def mark_password_changed(session: Session, when: dt.datetime | None = None) -> None:
    _put(session, PASSWORD_CHANGED_KEY, iso_utc(when or utcnow()) or "")


def settings_out(session: Session, env_minutes: int) -> dict:
    stored = stored_auto_lock(session)
    return {
        "auto_lock_minutes": stored if stored is not None else env_minutes,
        "auto_lock_choices": list(AUTO_LOCK_CHOICES),
        "auto_lock_source": "vault" if stored is not None else "env",
        "password_changed_at": password_changed_at(session),
        "paycheck_notify": paycheck_notify(session),
    }


# ------------------------------------------------------------------ vault hooks


def load_auto_lock(state: Any) -> None:
    """After every unlock: apply the saved auto-lock minutes to the vault (None = env value).

    Never raises (unlocking must not fail because of it); on a failure the env value applies.
    """
    vault = state.vault
    try:
        session = vault.db.acquire()
    except Exception:  # noqa: BLE001 - locked (DatabaseLocked) or locking
        return
    try:
        minutes = stored_auto_lock(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("could not read the auto-lock setting: %s", type(exc).__name__)
        minutes = None
    finally:
        vault.db.release(session)
    vault.set_auto_lock(minutes)


def record_password_changed(state: Any) -> None:
    """After setup, change-password and recover: remember when (never fails the caller)."""
    vault = state.vault
    try:
        session = vault.db.acquire()
    except Exception:  # noqa: BLE001
        return
    try:
        mark_password_changed(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("could not record the password change time: %s", type(exc).__name__)
    finally:
        vault.db.release(session)
