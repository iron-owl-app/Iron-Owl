"""Public keys that may sign FinTrack update files (``.ftupdate``), one set per update source.

``{key_id: base64 of the raw 32-byte Ed25519 public key}``, at most two entries per set (the
current key and, during a rotation, the next one).

- ``TRUSTED_KEYS``: the **file** source (updates sent as a file, found in Downloads or picked
  in Settings). The matching private key lives passphrase-encrypted outside the repo
  (``%USERPROFILE%\\.fintrack-release\\signing.pem``, made by ``tools/release/keygen.py``)
  with an offline copy in the owner's password manager.
- ``GITHUB_KEYS``: the **github** source (Iron Owl releases downloaded from GitHub). Shipped
  EMPTY: with no key, the update check says in plain words that updates can't be checked yet,
  and nothing is downloaded. To turn GitHub updates on, the owner (once, by hand):
    1. makes the key pair on their own PC with ``tools/release/keygen.py`` and a key id like
       ``io-2026a``;
    2. stores the private key as the GitHub repo secret the release workflow reads (never
       committed anywhere);
    3. pastes the printed public key below as ``"io-2026a": "<base64>"`` and commits it.
  Public keys are safe to commit. GitHub key ids are ``io-YYYYx``.

The two sets never mix: the file source trusts only ``TRUSTED_KEYS`` and the GitHub source
only ``GITHUB_KEYS`` (``keys_for``). A key id or key that is also in ``TRUSTED_KEYS`` is
dropped from the GitHub set.

Rotation: ship a release (signed by the old key) that lists old + new, then a release
signed by the new key that drops the old one.

With no trusted key every update file is rejected (FT-UPD-SIG), the safe failure. Tests
never add keys here; they monkeypatch an ephemeral key in, and a test asserts no test key
is ever committed.
"""
from __future__ import annotations

MAX_TRUSTED_KEYS = 2
KEY_ID_PATTERN = r"^ft-[0-9]{4}[a-z]$"  # file source, e.g. "ft-2099a"
GITHUB_KEY_ID_PATTERN = r"^io-[0-9]{4}[a-z]$"  # GitHub source, e.g. "io-2026a"
TEST_KEY_PREFIX = "test-"  # key ids tests use; must never appear below

TRUSTED_KEYS: dict[str, str] = {
}

GITHUB_KEYS: dict[str, str] = {
    "io-2026a": "zV0WslNvCy05Dt9A250yx1i5W1eg3bBneVfbCNl8AQg=",
}


def keys_for(source: str) -> dict[str, str]:
    """The keys an update from ``source`` may be signed with (read at call time, so tests
    can monkeypatch either set). Any other source trusts nothing."""
    if source == "file":
        return dict(TRUSTED_KEYS)
    if source == "github":
        file_values = set(TRUSTED_KEYS.values())
        return {k: v for k, v in GITHUB_KEYS.items() if k not in TRUSTED_KEYS and v not in file_values}
    return {}
