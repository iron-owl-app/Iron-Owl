"""Who to call for help (the "support contact"), set in Settings or by the installer.

The name shows on the unlock screen and the Help page, so it must be readable while the vault
is locked: it lives in a small plain file next to the vault, ``data/support_contact.json``
(``{"support_contact": "Sam 555-0100"}``), never inside the vault. Like ``data/limiter.json``
it holds nothing secret.

Precedence: the file when it exists and is usable (an empty value there means "no one
named"), else the settings value (``SUPPORT_CONTACT`` from the settings file, or the
launcher's ``state.json`` value passed in the environment). ``apply_saved`` copies the result
into ``settings.support_contact`` at startup and ``save`` after a change, so every reader of
the setting (``/api/auth/status``, ``/api/update/status``) shows the same name.

The packaged launcher reads the same file for its "couldn't start" message box, and
``packaging/install.ps1`` writes it when the installer is given a name.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import SUPPORT_CONTACT_MAX, Settings
from ..security import write_json_atomic

log = logging.getLogger("fintrack.support_contact")

FILE_NAME = "support_contact.json"
KEY = "support_contact"
# A real file is under 300 bytes; anything much bigger is not ours.
MAX_FILE_BYTES = 4096


class ContactError(ValueError):
    """The name can't be used (too long, or has characters that can't be shown)."""


def contact_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / FILE_NAME


def check(value: str) -> str:
    """The trimmed name, or ContactError. Unlike reading a file (which cleans quietly), a
    value typed in Settings is refused when it is too long or has control characters."""
    if not isinstance(value, str):
        raise ContactError("must be text")
    text = value.strip()
    if any(not ch.isprintable() for ch in text):
        raise ContactError("has characters that can't be shown")
    if len(text) > SUPPORT_CONTACT_MAX:
        raise ContactError(f"must be at most {SUPPORT_CONTACT_MAX} characters")
    return text


def clean(value: object) -> str:
    """Printable, trimmed, at most SUPPORT_CONTACT_MAX characters ("" when unusable): the
    same rule as the settings file and the launcher."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:SUPPORT_CONTACT_MAX].strip()


def read_saved(settings: Settings) -> str | None:
    """The saved name ("" = no one named), or None when there is no usable file."""
    path = contact_path(settings)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("support contact file unreadable (%s)", type(exc).__name__)
        return None
    if len(raw) > MAX_FILE_BYTES:
        log.warning("support contact file too large; ignored")
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        log.warning("support contact file unreadable; ignored")
        return None
    if not isinstance(data, dict) or not isinstance(data.get(KEY), str):
        log.warning("support contact file has no name; ignored")
        return None
    return clean(data[KEY])


def effective(settings: Settings) -> str:
    saved = read_saved(settings)
    return saved if saved is not None else clean(settings.support_contact)


def apply_saved(settings: Settings) -> None:
    """Startup: a name saved in Settings (or by the installer) wins over the settings file."""
    saved = read_saved(settings)
    if saved is not None:
        settings.support_contact = saved


def save(settings: Settings, value: str) -> str:
    """Check, write ``data/support_contact.json`` atomically, and use it at once."""
    text = check(value)
    path = contact_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {KEY: text})
    settings.support_contact = text
    return text
