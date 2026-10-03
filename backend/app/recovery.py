"""Recovery sheet: a printed 36-digit code that can set a new password (pure functions).

The code is 6 groups of 6 digits: 5 random digits and a Damm check digit computed over
``str(group_number) + data`` (group_number 1..6), so a typo is caught per group and a group
typed in the wrong box fails too. The 30 random digits (~99.7 bits) are stretched with
Argon2id into an X25519 private key; only its public key is stored. The vault key K is
sealed to that public key (ephemeral X25519 + HKDF-SHA256 + AES-256-GCM), so setting a new
password with the sheet never needs the old password and the server never stores the code.

A MAC keyed from K covers the whole block, so a block planted by someone who can write the
data folder (but doesn't know K) is never trusted for a re-seal. Making a new sheet rotates K
(``security.Vault.activate_pending``), so an old sheet's block, in any older keyfile copy,
unseals a key that opens nothing current.

Nothing here logs, and no exception message ever contains digits of the code.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field, replace

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

GROUPS = 6
GROUP_DIGITS = 6
DATA_DIGITS = 5
CODE_DIGITS = GROUPS * GROUP_DIGITS  # 36
MAX_RETIRED = 10

BLOCK_VERSION = 1
ALG = "x25519-hkdf-sha256-aes256gcm"
WRAP_INFO = b"fintrack/recovery-wrap/v1"
MAC_INFO = b"fintrack/recovery-mac/v1"
AAD_PREFIX = b"fintrack-recovery-v1|"
SHEET_PREFIX = b"fintrack-sheet"

REC_SALT_BYTES = 16
KEY_BYTES = 32
KDF_DEFAULTS = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}

# Damm's totally anti-symmetric quasigroup of order 10: catches every single-digit error
# and every adjacent transposition.
DAMM = (
    (0, 3, 1, 7, 5, 9, 8, 6, 4, 2),
    (7, 0, 9, 2, 1, 5, 4, 8, 6, 3),
    (4, 2, 0, 6, 8, 7, 1, 3, 5, 9),
    (1, 7, 5, 0, 9, 8, 3, 4, 2, 6),
    (6, 1, 2, 3, 0, 4, 5, 9, 7, 8),
    (3, 6, 7, 4, 2, 0, 9, 5, 8, 1),
    (5, 8, 6, 9, 7, 2, 0, 1, 3, 4),
    (8, 9, 4, 5, 3, 6, 2, 0, 1, 7),
    (9, 4, 3, 8, 6, 1, 7, 2, 0, 5),
    (2, 5, 8, 1, 4, 3, 6, 7, 9, 0),
)

_ALLOWED = re.compile(r"[0-9 \-]*")  # ASCII only: [0-9] is a literal range, unlike \d


class CodeFormatError(ValueError):
    """Not 36 ASCII digits (spaces and dashes aside). The message never includes the input."""

    def __init__(self) -> None:
        super().__init__("recovery code format")


class Unusable(Exception):
    """The public key matched but the sealed key can't be opened (tampered or damaged)."""


# ------------------------------------------------------------------ code format


def damm(digits: str) -> int:
    interim = 0
    for ch in digits:
        interim = DAMM[interim][ord(ch) - 48]
    return interim


def group_check(index: int, data5: str) -> str:
    """The check digit of group ``index`` (1-based) for its 5 data digits."""
    return str(damm(f"{index}{data5}"))


def group_ok(index: int, group6: str) -> bool:
    return damm(f"{index}{group6}") == 0


def generate_groups() -> list[str]:
    groups = []
    for i in range(1, GROUPS + 1):
        data = "".join(str(secrets.randbelow(10)) for _ in range(DATA_DIGITS))
        groups.append(data + group_check(i, data))
    return groups


def normalize(code: str) -> str:
    """Strip spaces and dashes; exactly 36 ASCII digits, else ``CodeFormatError``."""
    if not isinstance(code, str) or _ALLOWED.fullmatch(code) is None:
        raise CodeFormatError()
    digits = code.replace(" ", "").replace("-", "")
    if len(digits) != CODE_DIGITS:
        raise CodeFormatError()
    return digits


def split_groups(code36: str) -> list[str]:
    return [code36[i:i + GROUP_DIGITS] for i in range(0, CODE_DIGITS, GROUP_DIGITS)]


def bad_groups(code36: str) -> list[int]:
    """1-based numbers of the groups whose check digit is wrong."""
    return [i for i, g in enumerate(split_groups(code36), start=1) if not group_ok(i, g)]


def data_digits(code36: str) -> bytearray:
    """The 30 random digits as ASCII bytes (the Argon2id input)."""
    return bytearray("".join(g[:DATA_DIGITS] for g in split_groups(code36)).encode("ascii"))


def wipe(buf: bytearray | None) -> None:
    if buf is None:
        return
    for i in range(len(buf)):
        buf[i] = 0


# ------------------------------------------------------------------ keys


@dataclass(frozen=True)
class RecoveryKdf:
    salt: bytes
    time_cost: int
    memory_cost: int
    parallelism: int

    @classmethod
    def new(cls) -> RecoveryKdf:
        return cls(salt=secrets.token_bytes(REC_SALT_BYTES), **KDF_DEFAULTS)

    def valid(self) -> bool:
        # Untrusted file: bounded like KeyParams so a tampered block can't exhaust memory/CPU.
        return (
            len(self.salt) == REC_SALT_BYTES
            and 1 <= self.time_cost <= 20
            and 8 * 1024 <= self.memory_cost <= 1024 * 1024
            and 1 <= self.parallelism <= 16
        )

    def seed(self, data30: bytes | bytearray) -> bytearray:
        return bytearray(
            hash_secret_raw(
                secret=bytes(data30),
                salt=self.salt,
                time_cost=self.time_cost,
                memory_cost=self.memory_cost,
                parallelism=self.parallelism,
                hash_len=KEY_BYTES,
                type=Type.ID,
            )
        )


def private_key(seed: bytes | bytearray) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(bytes(seed))


def public_key(seed: bytes | bytearray) -> bytes:
    return private_key(seed).public_key().public_bytes_raw()


def sheet_number(pub: bytes) -> str:
    """Not secret: identifies a sheet ("482-913"), derived from its public key."""
    n = int.from_bytes(hashlib.sha256(SHEET_PREFIX + pub).digest()[:4], "big") % 1_000_000
    s = f"{n:06d}"
    return f"{s[:3]}-{s[3:]}"


def _wrap_key(shared: bytes, epk: bytes, pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=WRAP_INFO + epk + pub).derive(shared)


def _mac_key(key: bytes | bytearray) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=MAC_INFO).derive(bytes(key))


@dataclass(frozen=True)
class Sealed:
    for_salt: bytes  # the keyfile salt of the password key K that is sealed
    epk: bytes
    nonce: bytes
    ct: bytes
    alg: str = ALG


def seal(key: bytes | bytearray, pub: bytes, for_salt: bytes) -> Sealed:
    eph = X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes_raw()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(pub))
    nonce = os.urandom(12)
    ct = AESGCM(_wrap_key(shared, epk, pub)).encrypt(nonce, bytes(key), AAD_PREFIX + for_salt)
    return Sealed(for_salt=for_salt, epk=epk, nonce=nonce, ct=ct)


def unseal(seed: bytes | bytearray, pub: bytes, sealed: Sealed) -> bytearray:
    """Open the sealed key with the code's seed; the caller checked the public key matches."""
    try:
        priv = private_key(seed)
        if not hmac.compare_digest(priv.public_key().public_bytes_raw(), pub):
            raise Unusable()
        shared = priv.exchange(X25519PublicKey.from_public_bytes(sealed.epk))
        plain = AESGCM(_wrap_key(shared, sealed.epk, pub)).decrypt(
            sealed.nonce, sealed.ct, AAD_PREFIX + sealed.for_salt
        )
    except (ValueError, InvalidTag):
        raise Unusable() from None
    out = bytearray(plain)
    del plain
    return out


# ------------------------------------------------------------------ the keyfile block

_B64_LEN = {"salt": 16, "public_key": 32, "epk": 32, "nonce": 12, "ct": 48, "mac": 32}
_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
_BLOCK_KEYS = {
    "version", "created_at", "confirmed", "kdf", "salt", "time_cost", "memory_cost", "parallelism",
    "public_key", "sealed", "retired", "mac",
}
_SEALED_KEYS = {"alg", "for_salt", "epk", "nonce", "ct"}
_RETIRED_KEYS = {"public_key", "created_at", "retired_at"}


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object, length: int | None = None, *, min_len: int = 0, max_len: int = 0) -> bytes:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("bad base64")
    raw = base64.b64decode(value, validate=True)
    if _b64(raw) != value:  # canonical encoding only
        raise ValueError("bad base64")
    if length is not None and len(raw) != length:
        raise ValueError("bad length")
    if length is None and not (min_len <= len(raw) <= max_len):
        raise ValueError("bad length")
    return raw


def _int(value: object) -> int:
    if type(value) is not int:  # noqa: E721 - bool is an int subclass; refuse it
        raise ValueError("bad int")
    return value


def _ts(value: object) -> str:
    if not isinstance(value, str) or _TS.fullmatch(value) is None:
        raise ValueError("bad timestamp")
    return value


def _exact_keys(data: object, keys: set[str]) -> dict:
    if not isinstance(data, dict) or set(data) != keys:
        raise ValueError("bad keys")
    return data


def canonical_json(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@dataclass(frozen=True)
class Retired:
    public_key: bytes
    created_at: str
    retired_at: str

    def to_json(self) -> dict:
        return {"public_key": _b64(self.public_key), "created_at": self.created_at, "retired_at": self.retired_at}


@dataclass(frozen=True)
class RecoveryBlock:
    created_at: str
    confirmed: bool
    kdf: RecoveryKdf
    public_key: bytes
    sealed: Sealed
    retired: tuple[Retired, ...] = field(default_factory=tuple)
    mac: bytes = b""

    @property
    def sheet(self) -> str:
        return sheet_number(self.public_key)

    def unsigned_json(self) -> dict:
        return {
            "version": BLOCK_VERSION,
            "created_at": self.created_at,
            "confirmed": self.confirmed,
            "kdf": "argon2id",
            "salt": _b64(self.kdf.salt),
            "time_cost": self.kdf.time_cost,
            "memory_cost": self.kdf.memory_cost,
            "parallelism": self.kdf.parallelism,
            "public_key": _b64(self.public_key),
            "sealed": {
                "alg": self.sealed.alg,
                "for_salt": _b64(self.sealed.for_salt),
                "epk": _b64(self.sealed.epk),
                "nonce": _b64(self.sealed.nonce),
                "ct": _b64(self.sealed.ct),
            },
            "retired": [r.to_json() for r in self.retired],
        }

    def to_json(self) -> dict:
        return {**self.unsigned_json(), "mac": _b64(self.mac)}

    def _compute_mac(self, key: bytes | bytearray) -> bytes:
        return hmac.new(_mac_key(key), canonical_json(self.unsigned_json()), hashlib.sha256).digest()

    def signed(self, key: bytes | bytearray) -> RecoveryBlock:
        """The same block with its MAC computed under vault key ``key``."""
        return replace(self, mac=self._compute_mac(key))

    def mac_ok(self, key: bytes | bytearray | None) -> bool:
        if key is None or len(self.mac) != 32:
            return False
        return hmac.compare_digest(self._compute_mac(key), self.mac)

    def is_retired(self, pub: bytes) -> bool:
        return any(hmac.compare_digest(r.public_key, pub) for r in self.retired)

    def resealed(self, key: bytes | bytearray, for_salt: bytes) -> RecoveryBlock:
        """Seal ``key`` (whose keyfile salt is ``for_salt``) to the same sheet, MAC'd with it."""
        return replace(self, sealed=seal(key, self.public_key, for_salt)).signed(key)

    @classmethod
    def from_json(cls, data: object) -> RecoveryBlock:
        """Strict parse; raises ValueError (with a message that never includes the input)."""
        try:
            d = _exact_keys(data, _BLOCK_KEYS)
            if _int(d["version"]) != BLOCK_VERSION or d["kdf"] != "argon2id":
                raise ValueError("unsupported block")
            if not isinstance(d["confirmed"], bool):
                raise ValueError("bad confirmed")
            kdf = RecoveryKdf(
                salt=_unb64(d["salt"], _B64_LEN["salt"]),
                time_cost=_int(d["time_cost"]),
                memory_cost=_int(d["memory_cost"]),
                parallelism=_int(d["parallelism"]),
            )
            if not kdf.valid():
                raise ValueError("kdf out of range")
            s = _exact_keys(d["sealed"], _SEALED_KEYS)
            if s["alg"] != ALG:
                raise ValueError("bad alg")
            sealed = Sealed(
                for_salt=_unb64(s["for_salt"], min_len=16, max_len=64),
                epk=_unb64(s["epk"], _B64_LEN["epk"]),
                nonce=_unb64(s["nonce"], _B64_LEN["nonce"]),
                ct=_unb64(s["ct"], _B64_LEN["ct"]),
            )
            retired_raw = d["retired"]
            if not isinstance(retired_raw, list) or len(retired_raw) > MAX_RETIRED:
                raise ValueError("bad retired")
            retired = []
            for item in retired_raw:
                r = _exact_keys(item, _RETIRED_KEYS)
                retired.append(Retired(
                    public_key=_unb64(r["public_key"], _B64_LEN["public_key"]),
                    created_at=_ts(r["created_at"]),
                    retired_at=_ts(r["retired_at"]),
                ))
            return cls(
                created_at=_ts(d["created_at"]),
                confirmed=d["confirmed"],
                kdf=kdf,
                public_key=_unb64(d["public_key"], _B64_LEN["public_key"]),
                sealed=sealed,
                retired=tuple(retired),
                mac=_unb64(d["mac"], _B64_LEN["mac"]),
            )
        except ValueError:
            raise
        except Exception:  # noqa: BLE001 - untrusted input: any malformation is a ValueError
            raise ValueError("malformed recovery block") from None


def new_block(
    key: bytes | bytearray, pub: bytes, kdf: RecoveryKdf, for_salt: bytes, *,
    confirmed: bool, created_at: str | None = None, retired: tuple[Retired, ...] = (),
) -> RecoveryBlock:
    return RecoveryBlock(
        created_at=created_at or utc_now_iso(),
        confirmed=confirmed,
        kdf=kdf,
        public_key=pub,
        sealed=seal(key, pub, for_salt),
        retired=tuple(retired)[:MAX_RETIRED],
    ).signed(key)


def merge_retired(*lists: tuple[Retired, ...] | list[Retired], exclude: bytes) -> tuple[Retired, ...]:
    """Newest first, unique by public key, never the active one, at most MAX_RETIRED."""
    out: list[Retired] = []
    for items in lists:
        for r in items:
            if hmac.compare_digest(r.public_key, exclude):
                continue
            if any(hmac.compare_digest(r.public_key, o.public_key) for o in out):
                continue
            out.append(r)
    return tuple(out[:MAX_RETIRED])
