"""Make the Ed25519 key that signs FinTrack updates. Run ONCE, by the owner, on their own PC.

    backend\\.venv\\Scripts\\python.exe tools\\release\\keygen.py [--key-id ft-2099a]

Writes the private key, encrypted with a passphrase, to
``%USERPROFILE%\\.fintrack-release\\signing.pem`` (an ``--out`` inside the repo is
refused), refuses to overwrite an existing key, restricts the folder to the current user,
and prints the key id and public key to paste into ``backend/app/update_keys.py``.

The passphrase: press Enter to have one generated (``secrets.token_urlsafe(24)``, 32
characters, ~192 bits; printed ONCE -- put it in your password manager right away), or type
your own of at least 24 characters.

Why the length matters: ``cryptography``'s ``BestAvailableEncryption`` for a PKCS#8 PEM
uses PBKDF2-HMAC-SHA256 with a fixed 2048 iterations (not configurable for this format), so
the key file on its own offers little resistance to guessing a short or human-made
passphrase. A random 32-character passphrase makes that irrelevant; that is why it is the
default, and why a chosen one must be long.

Losing the key or its passphrase means an installed Iron Owl can no longer accept updates
(only a reinstall in person fixes that), so keep an offline copy of the .pem AND the
passphrase in your password manager.

Two kinds of key (Iron Owl 2.0.0), told apart by the id:

* ``ft-YYYYx`` signs the emailed updates (``TRUSTED_KEYS``); default file ``signing.pem``.
* ``io-YYYYx`` signs the public GitHub releases (``GITHUB_KEYS``); default file
  ``github-signing.pem``. Make it with ``--key-id io-2026a``, add the printed line to
  ``GITHUB_KEYS``, then store the .pem's text in the GitHub repository secret
  ``IRON_OWL_SIGNING_KEY`` and the passphrase in ``IRON_OWL_SIGNING_PASSPHRASE``. A GitHub key
  never replaces the emailed-update key.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import os
import re
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MIN_PASSPHRASE_CHARS = 24
GENERATED_PASSPHRASE_BYTES = 24  # secrets.token_urlsafe(24): 32 characters
KEY_ID_RE = re.compile(r"^(ft|io)-[0-9]{4}[a-z]$")  # ft-: emailed updates; io-: GitHub releases
REPO_ROOT = Path(__file__).resolve().parents[2]


def is_github_key_id(key_id: str) -> bool:
    return key_id.startswith("io-")


def default_key_path(key_id: str = "") -> Path:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    name = "github-signing.pem" if is_github_key_id(key_id) else "signing.pem"
    return Path(home) / ".fintrack-release" / name


def default_key_id() -> str:
    return f"ft-{dt.date.today().year}a"


def restrict_to_owner(folder: Path) -> bool:
    """Owner-only ACL on the key folder (same helper the app uses for data/)."""
    backend = str(REPO_ROOT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.security import restrict_to_owner as restrict

    return restrict(folder)


def inside_repo(path: Path, repo_root: Path = REPO_ROOT) -> bool:
    """True when ``path`` would land in the repository (where git, a backup or a sync could
    pick the key up). Compared on resolved, case-folded paths."""
    target = os.path.normcase(os.path.abspath(os.path.realpath(path)))
    root = os.path.normcase(os.path.abspath(os.path.realpath(repo_root)))
    try:
        return os.path.commonpath([target, root]) == root
    except ValueError:  # different drives
        return False


def generate_passphrase() -> str:
    """A random passphrase (32 URL-safe characters, ~192 bits)."""
    return secrets.token_urlsafe(GENERATED_PASSPHRASE_BYTES)


def check_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise ValueError(f"The passphrase must be at least {MIN_PASSPHRASE_CHARS} characters.")


def generate(
    dest: Path,
    passphrase: str,
    key_id: str,
    *,
    restrict: Callable[[Path], bool] = restrict_to_owner,
    repo_root: Path = REPO_ROOT,
) -> tuple[str, str]:
    """Create the encrypted key file; returns (key_id, base64 raw public key)."""
    check_passphrase(passphrase)
    if not KEY_ID_RE.fullmatch(key_id):
        raise ValueError("The key id must look like ft-2099a (emailed updates) or io-2099a (GitHub releases).")
    dest = Path(dest)
    if inside_repo(dest, repo_root):
        raise ValueError(f"{dest} is inside the repository; keep the signing key outside it.")
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"{dest} already exists; refusing to overwrite a signing key.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not restrict(dest.parent):
        print("WARNING: could not restrict the key folder to your account.", file=sys.stderr)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    with open(dest, "xb") as fh:
        fh.write(pem)
    return key_id, public_key_b64(key)


def load_private_key(path: Path, passphrase: str) -> Ed25519PrivateKey:
    """Raises ValueError for a wrong passphrase, TypeError for an unencrypted key."""
    data = Path(path).read_bytes()
    if b"ENCRYPTED" not in data:
        raise TypeError("The signing key is not passphrase-protected; refusing to use it.")
    key = serialization.load_pem_private_key(data, password=passphrase.encode("utf-8"))
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Not an Ed25519 key.")
    return key


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def choose_passphrase(ask: Callable[[str], str] = getpass.getpass) -> tuple[str, bool] | None:
    """(passphrase, generated?) or None when the two typed ones don't match / are too short."""
    first = ask(f"Your own passphrase (at least {MIN_PASSPHRASE_CHARS} characters), "
                "or press Enter to generate one (recommended): ")
    if not first:
        return generate_passphrase(), True
    try:
        check_passphrase(first)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return None
    if ask("Same passphrase again: ") != first:
        print("The passphrases don't match.", file=sys.stderr)
        return None
    return first, False


def main(argv: list[str] | None = None, *, ask: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description="Create the Iron Owl update signing key (once).")
    parser.add_argument("--key-id", default=default_key_id(),
                        help="ft-YYYYx: emailed updates (default); io-YYYYx: public GitHub releases")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.out is None:
        args.out = default_key_path(args.key_id)
    github = is_github_key_id(args.key_id)
    if inside_repo(args.out):
        print(f"{args.out} is inside the repository; keep the signing key outside it.", file=sys.stderr)
        return 1
    if args.out.exists():
        print(f"{args.out} already exists. Not overwriting it.", file=sys.stderr)
        return 1
    chosen = choose_passphrase(ask)
    if chosen is None:
        return 1
    passphrase, generated = chosen
    try:
        key_id, public = generate(args.out, passphrase, args.key_id)
    except (ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Signing key written to {args.out}")
    print(f"key_id:     {key_id}")
    print(f"public key: {public}")
    print()
    keys_name = "GITHUB_KEYS" if github else "TRUSTED_KEYS"
    print(f"Add this line to {keys_name} in backend/app/update_keys.py and commit it:")
    print(f'    "{key_id}": "{public}",')
    print()
    if generated:
        print("Your signing passphrase (shown only this once; it is not saved anywhere):")
        print()
        print(f"    {passphrase}")
        print()
        print("Store it in your password manager NOW, then clear this window.")
    print(f"Also save a copy of {args.out.name} in your password manager (or another offline place).")
    if github:
        print("For the GitHub release build: paste the whole .pem file into the repository secret")
        print("IRON_OWL_SIGNING_KEY and the passphrase into IRON_OWL_SIGNING_PASSPHRASE.")
    print("Without both, installed copies of Iron Owl can't accept updates until you reinstall them in person.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
