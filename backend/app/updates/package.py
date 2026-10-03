r"""Verify and extract signed FinTrack update files (``FinTrack-X.Y.Z.ftupdate``).

Format v2 -- a plain container, deliberately NOT a zip (mail filters such as Gmail block
zips that hold ``.js`` files, and look inside nested zips)::

    b"FTUPD\x00\x02"                          7-byte magic (format version 2)
    uint32 LE n1 + manifest.json   (1..64 KB)  what the update is; see ``Manifest``
    uint32 LE n2 + signature       (1..1 KB)   ``{"alg":"ed25519","key_id":"ft-2099a","sig":"<b64>"}``
    uint32 LE n3 + encoded payload (1..150 MB) payload.zip run through ``encode_payload``

The three sections fill the file exactly: no bytes before the magic, between sections or
after the payload. The signature is Ed25519 over ``SIGNATURE_DOMAIN + <exact manifest
bytes>``; its JSON must be byte-for-byte canonical (sorted keys, no spaces) with canonical
base64, so a signed update has exactly one valid file (and one id). The manifest binds the
ENCODED payload by SHA-256 and exact size.

The payload encoding is a fixed, public, parameter-free XOR with a SHAKE-256 counter
keystream seeded by a constant label. It is NOT encryption and hides nothing from anyone
who reads this file; it only keeps zip signatures and ``.js`` names out of the bytes a mail
filter sees. Integrity comes from the signature and hash, never from the encoding.

``verify_package`` checks, in this order, and stops at the first failure:

1.  the file is at most 150 MB (else FT-UPD-BIG);
2.  the magic (else FT-UPD-NOTUPD), then the section lengths add up exactly to the file
    size and manifest/payload are within their caps (FT-UPD-DMG); an empty or oversized
    signature is FT-UPD-SIG;
3.  the manifest and signature are read ONCE into memory, in the same sequential pass that
    hashes the whole file; the key id must be trusted and Ed25519 must verify -- any
    failure is FT-UPD-SIG and happens BEFORE the manifest is parsed;
4.  strict manifest parsing (no duplicate keys, no NaN/floats, no extra/missing fields,
    schema 2, app == "fintrack") else FT-UPD-NOTUPD;
5.  the encoded payload's exact size and SHA-256 must match the signed manifest
    (FT-UPD-DMG) -- for every version, so "older"/"same" are only ever reported for a file
    whose every byte is signed; it is kept in memory (and later decoded) only when newer;
6.  version <= current -> result "older" (FT-UPD-OLD) / "same" (FT-UPD-SAME), not an error
    (the payload is neither decoded nor inspected);
7.  the decoded payload's central directory, without extracting: bounded entries == manifest
    files, declared sizes <= unpacked_size <= cap for the kind, compression ratio, path rules
    and the whitelist (FT-UPD-DMG);
8.  requirements -> ``can_install`` / ``help`` (never a rejection).

Nothing parses a zip before the signature and the payload hash verified. Nothing here
executes, imports or writes anything from the file; ``extract_payload`` is the only writer
and extracts from the very in-memory copy that was verified (the file is never read twice,
so changing it on disk mid-install changes nothing).
"""
from __future__ import annotations

import base64
import binascii
import compileall
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import unicodedata
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .. import update_keys
from . import semver
from .requirements import Environment, Help
from .source import valid_repo
from .requirements import check as check_requirements

MB = 1024 * 1024
MAX_PACKAGE_BYTES = 150 * MB
MIN_PACKAGE_BYTES = 200  # smaller than any real package; the Downloads scan skips these
MAGIC = b"FTUPD\x00\x02"  # "FTUPD", NUL, container format 2
MANIFEST_SCHEMA = 2
RELEASE_NAME = "release.json"
_LEN = struct.Struct("<I")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIG_BYTES = 1024
MAX_PAYLOAD_BYTES = MAX_PACKAGE_BYTES
MAX_PAYLOAD_ENTRIES = 20_000
MAX_PAYLOAD_CD_BYTES = 8 * MB
UNPACKED_CAPS = {"app": 60 * MB, "full": 500 * MB}
RATIO_LIMIT = 100
RATIO_MIN_BYTES = 1 * MB
MAX_PATH_CHARS = 200
MAX_RELEASE_JSON_BYTES = 4096
SIGNATURE_DOMAIN = b"FinTrack update manifest v2\x00"
SIG_ALG = "ed25519"
SIG_KEYS = frozenset({"alg", "key_id", "sig"})
CHUNK = 1 * MB
# The payload encoding (see the module docstring): public, fixed, NOT encryption.
PAYLOAD_ENCODING_LABEL = b"FinTrack update payload encoding v2 (not encryption)"
_ENCODING_BLOCK = 64 * 1024

RejectReason = Literal["signature", "not_update", "damaged", "too_large"]
REJECT_CODES: dict[str, str] = {
    "signature": "FT-UPD-SIG",
    "not_update": "FT-UPD-NOTUPD",
    "damaged": "FT-UPD-DMG",
    "too_large": "FT-UPD-BIG",
}
RESULT_CODES = {"older": "FT-UPD-OLD", "same": "FT-UPD-SAME"}
INSTALL_FAILED_CODE = "FT-UPD-02"

_ZIP_ERRORS = (
    zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, OSError, EOFError, ValueError,
    NotImplementedError, RuntimeError, struct.error, OverflowError,
)


class UpdateRejected(Exception):
    """The file must not be installed. ``str(exc)`` is only the code (safe to log)."""

    def __init__(self, reason: RejectReason) -> None:
        self.reason = reason
        self.code = REJECT_CODES[reason]
        super().__init__(self.code)


class NotInstallable(Exception):
    """A genuine update that can't be installed here (older/same, or needs help)."""

    def __init__(self, verified: VerifiedPackage) -> None:
        self.verified = verified
        self.code = verified.code or (verified.help.code if verified.help else "FT-UPD-HELP")
        super().__init__(self.code)


class ExtractError(Exception):
    """Extraction failed; the partial folder was removed (FT-UPD-02, nothing changed)."""

    code = INSTALL_FAILED_CODE


# ------------------------------------------------------------------ container


def _keystream_block(index: int, length: int) -> bytes:
    return hashlib.shake_256(PAYLOAD_ENCODING_LABEL + index.to_bytes(8, "little")).digest(length)


def transform_payload_inplace(buf: bytearray) -> None:
    """XOR ``buf`` with the fixed keystream (encoding and decoding are the same step).

    Keystream block i (64 KiB) = SHAKE-256(PAYLOAD_ENCODING_LABEL || uint64le(i)). Public and
    constant: this is NOT encryption, only a way to keep zip/script bytes out of the file.
    """
    view = memoryview(buf)
    for index, start in enumerate(range(0, len(buf), _ENCODING_BLOCK)):
        chunk = view[start:start + _ENCODING_BLOCK]
        n = len(chunk)
        mixed = int.from_bytes(chunk, "little") ^ int.from_bytes(_keystream_block(index, n), "little")
        chunk[:] = mixed.to_bytes(n, "little")


def encode_payload(payload_zip: bytes) -> bytes:
    """payload.zip -> the bytes stored in the container (the release tool uses this)."""
    buf = bytearray(payload_zip)
    transform_payload_inplace(buf)
    return bytes(buf)


def decode_payload(encoded: bytes) -> bytes:
    """The inverse of ``encode_payload`` (the same XOR)."""
    return encode_payload(encoded)


def pack_container(manifest: bytes, signature: bytes, encoded_payload: bytes) -> bytes:
    """MAGIC + three uint32-LE-length-prefixed sections: exactly a v2 ``.ftupdate``."""
    out = bytearray(MAGIC)
    for section in (manifest, signature, encoded_payload):
        out += _LEN.pack(len(section))
        out += section
    return bytes(out)


# ------------------------------------------------------------------ manifest


@dataclass(frozen=True)
class PayloadInfo:
    size: int  # of the ENCODED payload section
    sha256: str  # of the ENCODED payload section
    files: int
    unpacked_size: int


@dataclass(frozen=True)
class Manifest:
    schema: int
    app: str
    version: str
    released_at: str
    min_current_version: str
    requires_reinstall: bool
    kind: Literal["app", "full"]
    python: str
    bootstrap_version: int
    schema_version: int
    deps_id: str
    notes: tuple[str, ...]
    payload: PayloadInfo

    @property
    def display_version(self) -> str:
        return semver.display(self.version)

    @property
    def minutes(self) -> int:
        return 1 if self.kind == "app" else 3


MANIFEST_KEYS = frozenset({
    "schema", "app", "version", "released_at", "min_current_version", "requires_reinstall",
    "kind", "python", "bootstrap_version", "schema_version", "deps_id", "notes", "payload",
})
PAYLOAD_KEYS = frozenset({"size", "sha256", "files", "unpacked_size"})
RELEASE_KEYS = frozenset({"version", "schema_version", "deps_id", "kind", "python", "bootstrap_version"})
# Optional in release.json since 2.0.0 (GitHub builds write both; emailed-file builds write
# neither, because versions before 2.0.0 refuse any key beyond RELEASE_KEYS). See source.py.
OPTIONAL_RELEASE_KEYS = frozenset({"update_source", "update_repo"})
_RELEASED_AT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PYTHON_RE = re.compile(r"^3\.[0-9]{1,2}$")
_DEPS_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_NOTES, MAX_NOTE_CHARS = 6, 160
_BAD_NOTE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _reject_constant(_name: str):
    raise ValueError("non-finite number")


def _reject_float(_text: str):
    raise ValueError("floats are not allowed")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def strict_json(raw: bytes) -> object:
    """UTF-8 (no BOM) JSON without duplicate keys, floats, NaN or Infinity."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("BOM")
    text = raw.decode("utf-8")
    return json.loads(
        text, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_reject_float,
    )


def canonical_json(data: dict) -> bytes:
    """Sorted keys, no spaces, ASCII: the one encoding the builder writes."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _is_int(value: object, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _valid_note(note: object) -> bool:
    if not isinstance(note, str) or not 1 <= len(note) <= MAX_NOTE_CHARS or not note.strip():
        return False
    return not any(unicodedata.category(ch) in _BAD_NOTE_CATEGORIES for ch in note)


def _parse_manifest(raw: bytes) -> Manifest:
    """Signed bytes -> Manifest; anything unexpected is FT-UPD-NOTUPD. Only call after
    the signature verified (tests spy on this to prove it)."""
    try:
        data = strict_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise UpdateRejected("not_update") from None
    if not isinstance(data, dict) or set(data) != MANIFEST_KEYS:
        raise UpdateRejected("not_update")
    payload = data["payload"]
    ok = (
        data["app"] == "fintrack"
        and _is_int(data["schema"], MANIFEST_SCHEMA, MANIFEST_SCHEMA)
        and semver.is_valid(data["version"])
        and isinstance(data["released_at"], str) and _RELEASED_AT_RE.fullmatch(data["released_at"]) is not None
        and semver.is_valid(data["min_current_version"])
        and isinstance(data["requires_reinstall"], bool)
        and data["kind"] in ("app", "full")
        and isinstance(data["python"], str) and _PYTHON_RE.fullmatch(data["python"]) is not None
        and _is_int(data["bootstrap_version"], 1, 1000)
        and _is_int(data["schema_version"], 0, 100_000)
        and isinstance(data["deps_id"], str) and _DEPS_ID_RE.fullmatch(data["deps_id"]) is not None
        and isinstance(data["notes"], list) and 1 <= len(data["notes"]) <= MAX_NOTES
        and all(_valid_note(n) for n in data["notes"])
        and isinstance(payload, dict) and set(payload) == PAYLOAD_KEYS
    )
    if not ok:
        raise UpdateRejected("not_update")
    ok = (
        _is_int(payload["size"], 1, MAX_PAYLOAD_BYTES)
        and isinstance(payload["sha256"], str) and _SHA256_RE.fullmatch(payload["sha256"]) is not None
        and _is_int(payload["files"], 1, MAX_PAYLOAD_ENTRIES)
        and _is_int(payload["unpacked_size"], 0, 10**12)
    )
    if not ok:
        raise UpdateRejected("not_update")
    return Manifest(
        schema=data["schema"], app=data["app"], version=data["version"], released_at=data["released_at"],
        min_current_version=data["min_current_version"], requires_reinstall=data["requires_reinstall"],
        kind=data["kind"], python=data["python"], bootstrap_version=data["bootstrap_version"],
        schema_version=data["schema_version"], deps_id=data["deps_id"], notes=tuple(data["notes"]),
        payload=PayloadInfo(payload["size"], payload["sha256"], payload["files"], payload["unpacked_size"]),
    )


def manifest_to_dict(m: Manifest) -> dict:
    """The manifest as the builder writes it (tests and the release tool use this)."""
    return {
        "schema": m.schema, "app": m.app, "version": m.version, "released_at": m.released_at,
        "min_current_version": m.min_current_version, "requires_reinstall": m.requires_reinstall,
        "kind": m.kind, "python": m.python, "bootstrap_version": m.bootstrap_version,
        "schema_version": m.schema_version, "deps_id": m.deps_id, "notes": list(m.notes),
        "payload": {"size": m.payload.size, "sha256": m.payload.sha256, "files": m.payload.files,
                    "unpacked_size": m.payload.unpacked_size},
    }


# ------------------------------------------------------------------ signature


def _b64(text: object, length: int) -> bytes:
    """Strict, canonical base64 of exactly ``length`` bytes (one spelling per value)."""
    if not isinstance(text, str) or len(text) > 256:
        raise ValueError("bad base64")
    raw = base64.b64decode(text.encode("ascii"), validate=True)
    if len(raw) != length:
        raise ValueError("bad length")
    if base64.b64encode(raw).decode("ascii") != text:
        raise ValueError("non-canonical base64")  # e.g. non-zero padding bits
    return raw


def _check_signature(manifest_raw: bytes, sig_raw: bytes, trusted_keys: Mapping[str, str]) -> str:
    """Raises FT-UPD-SIG unless a trusted key signed exactly these bytes; returns key id."""
    try:
        sig = strict_json(sig_raw)
        if not isinstance(sig, dict) or set(sig) != SIG_KEYS or sig["alg"] != SIG_ALG:
            raise ValueError("bad signature file")
        if canonical_json(sig) != sig_raw:
            raise ValueError("signature file not canonical")
        key_id = sig["key_id"]
        if not isinstance(key_id, str) or key_id not in trusted_keys:
            raise ValueError("untrusted key")
        signature = _b64(sig["sig"], 64)
        public = Ed25519PublicKey.from_public_bytes(_b64(trusted_keys[key_id], 32))
        public.verify(signature, SIGNATURE_DOMAIN + manifest_raw)
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, InvalidSignature, RecursionError):
        raise UpdateRejected("signature") from None
    return key_id


# ------------------------------------------------------------------ path rules


_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    # COM0-9 / LPT0-9, and superscript 1-3
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in [*"0123456789", chr(0xB9), chr(0xB2), chr(0xB3)]}
)
_BAD_PATH_CHARS = frozenset('\\:<>"|?*')
_PY_SEGMENT = re.compile(r"^[a-z0-9_]+$")
_PY_FILE = re.compile(r"^[a-z0-9_]+\.py$")
WEB_EXTENSIONS = frozenset({"html", "js", "css", "svg", "png", "ico", "webmanifest", "json", "woff", "woff2", "txt"})
TOP_LEVEL_FILES = frozenset({RELEASE_NAME, "run_packaged.py"})


def path_problem(name: object) -> str | None:
    """Why an entry name is unsafe as a relative path on Windows, or None when it's fine."""
    if not isinstance(name, str) or not name:
        return "empty"
    if len(name) > MAX_PATH_CHARS:
        return "too long"
    for ch in name:
        code = ord(ch)
        if ch in _BAD_PATH_CHARS or code < 32 or 0x7F <= code <= 0x9F:
            return "bad character"
    if name.startswith("/"):
        return "absolute"
    for segment in name.split("/"):
        if segment in ("", ".", ".."):
            return "empty or dot segment"
        if segment.endswith((".", " ")):
            return "segment ends with dot or space"
        if segment.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
            return "reserved name"
    return None


def whitelisted(name: str, kind: str) -> bool:
    """Only these may be in a payload: release.json, run_packaged.py, backend/app/**/*.py,
    web/** with a known extension, and site-packages/** for kind=full."""
    if name in TOP_LEVEL_FILES:
        return True
    parts = name.split("/")
    if len(parts) >= 3 and parts[0] == "backend" and parts[1] == "app":
        return all(_PY_SEGMENT.fullmatch(p) for p in parts[2:-1]) and _PY_FILE.fullmatch(parts[-1]) is not None
    if len(parts) >= 2 and parts[0] == "web":
        stem, dot, ext = parts[-1].rpartition(".")
        return bool(dot and stem) and ext in WEB_EXTENSIONS
    if len(parts) >= 2 and parts[0] == "site-packages":
        return kind == "full"
    return False


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def entry_problem(info: zipfile.ZipInfo, kind: str) -> str | None:
    """Why a payload entry must not be installed, or None."""
    if info.orig_filename != info.filename:  # zipfile silently rewrote "\" or cut at NUL
        return "name rewritten"
    problem = path_problem(info.filename)
    if problem:
        return problem
    if info.is_dir() or not whitelisted(info.filename, kind):
        return "not whitelisted"
    if info.flag_bits & 0x41:
        return "encrypted"
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        return "compression"
    if stat.S_ISLNK(info.external_attr >> 16):
        return "symlink"
    if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
        return "stored size mismatch"
    if info.file_size > RATIO_MIN_BYTES and info.file_size > RATIO_LIMIT * max(info.compress_size, 1):
        return "compression ratio"
    return None


# ------------------------------------------------------------------ verification


@dataclass(frozen=True)
class VerifiedPackage:
    result: Literal["ready", "older", "same"]
    manifest: Manifest
    sha256: str  # of the whole .ftupdate file (exactly the bytes that were verified)
    size: int
    can_install: bool
    help: Help | None
    key_id: str

    @property
    def id(self) -> str:
        """The offer id: first 16 hex digits of the file's SHA-256."""
        return self.sha256[:16]

    @property
    def code(self) -> str | None:
        return RESULT_CODES.get(self.result)


class _MemReader(io.RawIOBase):
    """A read-only, seekable file over a private in-memory buffer (the decoded payload)."""

    def __init__(self, buf: bytearray) -> None:
        super().__init__()
        self._view, self._pos = memoryview(buf), 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            pos = offset
        elif whence == 1:
            pos = self._pos + offset
        elif whence == 2:
            pos = len(self._view) + offset
        else:
            raise ValueError("whence")
        if pos < 0:
            raise OSError("negative seek")
        self._pos = pos
        return pos

    def readinto(self, buffer) -> int:
        n = min(len(buffer), len(self._view) - self._pos)
        if n <= 0:
            return 0
        buffer[:n] = self._view[self._pos:self._pos + n]
        self._pos += n
        return n


def _read_exact(fh: BinaryIO, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise UpdateRejected("damaged")
    return data


@dataclass(frozen=True)
class _Head:
    """Everything before the payload, read once: the exact bytes and the parsed sections."""

    raw: bytes
    manifest: bytes
    signature: bytes
    payload_len: int


def _section_lengths(fh: BinaryIO, size: int) -> tuple[int, int, int]:
    """Walk the three length fields (reading nothing else) and require they fill the file."""
    lengths: list[int] = []
    pos = len(MAGIC)
    for _ in range(3):
        fh.seek(pos)
        (n,) = _LEN.unpack(_read_exact(fh, _LEN.size))
        pos += _LEN.size
        if n > size - pos:
            raise UpdateRejected("damaged")  # runs past the end: cut off or forged
        lengths.append(n)
        pos += n
    if pos != size:
        raise UpdateRejected("damaged")  # trailing bytes
    return lengths[0], lengths[1], lengths[2]


def _read_head(fh: BinaryIO, size: int) -> _Head:
    fh.seek(0)
    if fh.read(len(MAGIC)) != MAGIC:
        raise UpdateRejected("not_update")
    n_manifest, n_sig, n_payload = _section_lengths(fh, size)
    if not 1 <= n_manifest <= MAX_MANIFEST_BYTES or not 1 <= n_payload <= MAX_PAYLOAD_BYTES:
        raise UpdateRejected("damaged")
    if not 1 <= n_sig <= MAX_SIG_BYTES:
        raise UpdateRejected("signature")  # unsigned, or not a signature
    # One sequential read of everything before the payload; parse ONLY these bytes (and
    # require the same lengths, in case the file changed since the walk above).
    head_len = len(MAGIC) + 3 * _LEN.size + n_manifest + n_sig
    fh.seek(0)
    raw = _read_exact(fh, head_len)
    pos = len(MAGIC)
    sections: list[bytes] = []
    for expected in (n_manifest, n_sig):
        (n,) = _LEN.unpack_from(raw, pos)
        if n != expected:
            raise UpdateRejected("damaged")
        sections.append(raw[pos + _LEN.size: pos + _LEN.size + n])
        pos += _LEN.size + n
    (n,) = _LEN.unpack_from(raw, pos)
    if raw[:len(MAGIC)] != MAGIC or n != n_payload or pos + _LEN.size != head_len:
        raise UpdateRejected("damaged")
    return _Head(raw, sections[0], sections[1], n_payload)


def _read_payload(fh: BinaryIO, n: int, file_digest, *, keep: bool) -> tuple[bytearray | None, str]:
    """Continue the sequential pass: read the ``n`` payload bytes at the current position,
    hashing them into ``file_digest`` and their own digest. Returns (bytes if keep, hex)."""
    digest = hashlib.sha256()
    buf = bytearray(n) if keep else None
    view = memoryview(buf) if keep else None
    scratch = bytearray(min(CHUNK, n))
    pos = 0
    while pos < n:
        want = min(CHUNK, n - pos)
        target = view[pos:pos + want] if view is not None else memoryview(scratch)[:want]
        got = fh.readinto(target)
        if not got:
            raise UpdateRejected("damaged")  # shorter than when we looked
        block = target[:got]
        digest.update(block)
        file_digest.update(block)
        pos += got
    return buf, digest.hexdigest()


def _check_payload(decoded: bytearray, manifest: Manifest) -> None:
    """The payload's central directory, without extracting anything (FT-UPD-DMG)."""
    reader = _MemReader(decoded)
    endrec = _end_record(reader)
    if not endrec:
        raise UpdateRejected("damaged")
    entries = endrec[zipfile._ECD_ENTRIES_TOTAL]  # noqa: SLF001
    if (
        entries > MAX_PAYLOAD_ENTRIES
        or entries != manifest.payload.files
        or endrec[zipfile._ECD_SIZE] > MAX_PAYLOAD_CD_BYTES  # noqa: SLF001
    ):
        raise UpdateRejected("damaged")
    if manifest.payload.unpacked_size > UNPACKED_CAPS[manifest.kind]:
        raise UpdateRejected("damaged")
    try:
        reader.seek(0)
        with zipfile.ZipFile(reader) as inner:
            infos = inner.infolist()
    except _ZIP_ERRORS:
        raise UpdateRejected("damaged") from None
    if len(infos) != manifest.payload.files:
        raise UpdateRejected("damaged")
    seen: set[str] = set()
    total = 0
    for info in infos:
        if entry_problem(info, manifest.kind):
            raise UpdateRejected("damaged")
        key = _fold(info.filename)
        if key in seen:
            raise UpdateRejected("damaged")
        seen.add(key)
        total += info.file_size
    if total > manifest.payload.unpacked_size or RELEASE_NAME not in seen:
        raise UpdateRejected("damaged")


def _end_record(fh) -> list | None:
    try:
        fh.seek(0)
        return zipfile._EndRecData(fh)  # noqa: SLF001 - exactly what ZipFile.__init__ reads
    except _ZIP_ERRORS:
        return None


HEAD_PREFIX_BYTES = CHUNK  # every valid head (<= ~66 KB) lies within the first block


def check_head(prefix: bytes, size: int, trusted_keys: Mapping[str, str] | None = None) -> None:
    """Steps 1-3 of ``verify_package`` on the first bytes of a file that is ``size`` bytes
    long: magic, a length walk that fills ``size`` exactly, caps, and the signature. Raises
    UpdateRejected; nothing is parsed beyond the signature.

    For the Downloads copy: a file that fails here is not copied any further. ``prefix`` must
    be the file's first ``min(size, HEAD_PREFIX_BYTES)`` bytes; a length field beyond it can
    only belong to an over-cap section and is FT-UPD-DMG. ``verify_package`` on the full
    copy stays the only check that decides anything.
    """
    keys = update_keys.TRUSTED_KEYS if trusted_keys is None else trusted_keys
    if size > MAX_PACKAGE_BYTES:
        raise UpdateRejected("too_large")
    if not prefix.startswith(MAGIC):
        raise UpdateRejected("not_update")
    try:
        head = _read_head(io.BytesIO(prefix), size)  # a read past ``prefix`` comes back short
    except (OSError, struct.error):
        raise UpdateRejected("damaged") from None
    _check_signature(head.manifest, head.signature, keys)


def _verify_open(
    fh: BinaryIO,
    current_version: str,
    trusted_keys: Mapping[str, str] | None,
    env: Environment | None,
) -> tuple[VerifiedPackage, bytearray | None]:
    """All checks on one open file, in one sequential pass. Returns the result and, for
    "ready", the DECODED payload.zip bytes that were verified (extraction uses only these)."""
    keys = update_keys.TRUSTED_KEYS if trusted_keys is None else trusted_keys
    size = os.fstat(fh.fileno()).st_size
    if size > MAX_PACKAGE_BYTES:
        raise UpdateRejected("too_large")
    try:
        # 2. Structure: magic, exact lengths, caps. No section content is interpreted yet.
        head = _read_head(fh, size)
    except (OSError, struct.error):
        raise UpdateRejected("damaged") from None
    file_digest = hashlib.sha256(head.raw)

    # 3. Signature over the exact bytes, before anything parses the manifest.
    key_id = _check_signature(head.manifest, head.signature, keys)

    # 4. Parse (downgrades and replays are answered after the payload hash below).
    manifest = _parse_manifest(head.manifest)
    order = semver.compare(manifest.version, current_version)
    # 5. The encoded payload is exactly what the signed manifest says (size and SHA-256),
    # BEFORE it is decoded -- also for older/same, so every non-error result is a file whose
    # every byte is covered by the signature (its id names exactly the signed release).
    if head.payload_len != manifest.payload.size:
        raise UpdateRejected("damaged")
    try:
        encoded, digest = _read_payload(fh, head.payload_len, file_digest, keep=order > 0)
    except OSError:
        raise UpdateRejected("damaged") from None
    if not hmac.compare_digest(digest, manifest.payload.sha256):
        raise UpdateRejected("damaged")
    # 6. Refuse downgrades and replays (not an error; nothing is decoded).
    if order <= 0:
        result: Literal["older", "same"] = "older" if order < 0 else "same"
        return VerifiedPackage(result, manifest, file_digest.hexdigest(), size, False, None, key_id), None
    if encoded is None:  # unreachable: kept whenever order > 0
        raise UpdateRejected("damaged")
    decoded = encoded  # decoded in place: from here on only this private copy is used
    transform_payload_inplace(decoded)

    # 7. What's inside the payload, without extracting it.
    _check_payload(decoded, manifest)

    # 8. Requirements never reject: they decide can_install / help.
    help_ = check_requirements(manifest, current_version, env)
    verified = VerifiedPackage("ready", manifest, file_digest.hexdigest(), size, help_ is None, help_, key_id)
    return verified, decoded


def verify_package(
    path: str | os.PathLike,
    current_version: str,
    *,
    trusted_keys: Mapping[str, str] | None = None,
    env: Environment | None = None,
) -> VerifiedPackage:
    """Verify an update file; raises UpdateRejected (reason + FT-UPD-* code).

    ``current_version`` must be a valid version (ValueError otherwise). ``trusted_keys``
    defaults to ``update_keys.TRUSTED_KEYS`` read at call time. ``env`` enables the
    install-root checks (runtime, launcher, disk); without it only manifest-level help
    reasons are found. Raises OSError if the file can't be opened.
    """
    semver.parse(current_version)
    with open(path, "rb", buffering=0) as fh:  # unbuffered: every byte checked is a byte read
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise UpdateRejected("not_update")
        verified, _ = _verify_open(fh, current_version, trusted_keys, env)
    return verified


# ------------------------------------------------------------------ extraction


@dataclass(frozen=True)
class ExtractedVersion:
    path: Path  # versions_dir / <version>
    manifest: Manifest
    verified: VerifiedPackage
    runtime: Path | None  # runtimes_dir/<deps_id>/site-packages for kind=full


def long_path(path: str | os.PathLike) -> str:
    r"""An absolute path that Windows won't cut at 260 characters (``\\?\`` prefix).

    Elsewhere (and for paths already prefixed) it is just the absolute path."""
    text = os.path.abspath(os.fspath(path))
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _safe_target(root: Path, name: str) -> Path:
    target = root.joinpath(*name.split("/"))
    root_abs, target_abs = os.path.abspath(root), os.path.abspath(target)
    if target_abs == root_abs or os.path.commonpath([root_abs, target_abs]) != root_abs:
        raise ExtractError("outside target")
    return target


def _check_release_json(path: Path, m: Manifest) -> None:
    try:
        with open(long_path(path), "rb") as fh:
            raw = fh.read(MAX_RELEASE_JSON_BYTES + 1)
        if len(raw) > MAX_RELEASE_JSON_BYTES:
            raise ValueError("too large")
        data = strict_json(raw)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise ExtractError("release.json unreadable") from None
    expected = {
        "version": m.version, "schema_version": m.schema_version, "deps_id": m.deps_id,
        "kind": m.kind, "python": m.python, "bootstrap_version": m.bootstrap_version,
    }
    if (
        not isinstance(data, dict)
        or not RELEASE_KEYS <= set(data) <= RELEASE_KEYS | OPTIONAL_RELEASE_KEYS
        or any(type(data[k]) is not type(v) or data[k] != v for k, v in expected.items())
        or not release_source_ok(data)
    ):
        raise ExtractError("release.json does not match the manifest")


def release_source_ok(data: dict) -> bool:
    """The optional update-source keys of a release.json: absent, or ``update_source`` is
    "file" (no repo) or "github" with a valid ``update_repo``."""
    if "update_source" not in data:
        return "update_repo" not in data
    source = data["update_source"]
    if source == "file":
        return "update_repo" not in data
    return source == "github" and valid_repo(data.get("update_repo"))


def extract_payload(
    package_path: str | os.PathLike,
    current_version: str,
    versions_dir: Path,
    *,
    trusted_keys: Mapping[str, str] | None = None,
    env: Environment | None = None,
    runtimes_dir: Path | None = None,
    compile_bytecode: bool = False,
) -> ExtractedVersion:
    r"""Re-verify the package, then install its files as ``versions_dir/<version>``.

    The file is read once; the payload is extracted from the verified in-memory copy (a
    change to the file on disk after it was read can't reach the install). Extracts into
    ``versions_dir/.partial-<v>-<hex>/`` with every name re-checked and joined under that
    folder, each file opened ``"xb"`` through a ``\\?\`` long path (never overwrites), real
    bytes counted against the declared sizes; ``release.json`` must match the manifest; then
    one ``os.replace``. On any failure the partial folder is removed and ExtractError (or
    UpdateRejected / NotInstallable from the re-verification) is raised. For kind=full with
    ``runtimes_dir`` the ``site-packages`` folder becomes ``runtimes_dir/<deps_id>/site-packages``.
    """
    semver.parse(current_version)
    versions_dir = Path(versions_dir)
    with open(package_path, "rb", buffering=0) as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise UpdateRejected("not_update")
        verified, decoded = _verify_open(fh, current_version, trusted_keys, env)
    if verified.result != "ready" or not verified.can_install or decoded is None:
        raise NotInstallable(verified)
    m = verified.manifest
    final = versions_dir / m.version
    if final.exists() or final.is_symlink():
        raise ExtractError("version folder already exists")
    os.makedirs(long_path(versions_dir), exist_ok=True)
    partial = versions_dir / f".partial-{m.version}-{secrets.token_hex(8)}"
    os.mkdir(long_path(partial))
    try:
        runtime = _extract_into(decoded, m, partial, runtimes_dir, compile_bytecode)
        os.replace(long_path(partial), long_path(final))
    except ExtractError:
        shutil.rmtree(long_path(partial), ignore_errors=True)
        raise
    except (*_ZIP_ERRORS, UpdateRejected) as exc:
        shutil.rmtree(long_path(partial), ignore_errors=True)
        raise ExtractError(type(exc).__name__) from None
    return ExtractedVersion(final, m, verified, runtime)


def _extract_into(
    decoded: bytearray, m: Manifest, partial: Path, runtimes_dir: Path | None, compile_bytecode: bool,
) -> Path | None:
    written = 0
    with zipfile.ZipFile(_MemReader(decoded)) as inner:
        infos = inner.infolist()
        if len(infos) != m.payload.files:
            raise ExtractError("entry count")
        for info in infos:
            if entry_problem(info, m.kind):
                raise ExtractError("unsafe entry")
            target = long_path(_safe_target(partial, info.filename))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            count = 0
            with inner.open(info) as src, open(target, "xb") as dst:
                while block := src.read(CHUNK):
                    count += len(block)
                    written += len(block)
                    if count > info.file_size or written > m.payload.unpacked_size:
                        raise ExtractError("more bytes than declared")
                    dst.write(block)
            if count != info.file_size:
                raise ExtractError("fewer bytes than declared")
    _check_release_json(partial / RELEASE_NAME, m)
    app_dir = partial / "backend" / "app"
    # through the long path too: a .pyc under __pycache__ can pass 260 characters
    if compile_bytecode and app_dir.is_dir() and not compileall.compile_dir(long_path(app_dir), quiet=1):
        raise ExtractError("compile failed")
    site = partial / "site-packages"
    if m.kind != "full" or runtimes_dir is None or not site.is_dir():
        return None
    dest = Path(runtimes_dir) / m.deps_id / "site-packages"
    if dest.exists():
        shutil.rmtree(long_path(site))  # same deps_id, same packages: keep the one already there
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(long_path(site), long_path(dest))
    return dest
