"""Encrypted backup files (.ftbackup) and validation of uploaded ones.

A backup is a zip with exactly three entries:
- ``fintrack.db``: a ``VACUUM INTO`` copy, still SQLCipher-encrypted with the vault key;
- ``keyfile.json``: the (non-secret) KDF salt and parameters;
- ``manifest.json``: ``{format: 1, created_at, schema_version, recovery_sheet}``.
Without the master password (or the recovery sheet whose block is in the keyfile) the file
is useless.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import secrets
import shutil
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from .. import db as dbmod
from ..migrations import LATEST
from ..recovery import RecoveryBlock
from ..security import BadBackup, Vault, VaultError, read_keyfile, restrict_to_owner
from ..utils import iso_utc, utcnow

FORMAT = 1
DB_ENTRY, KEYFILE_ENTRY, MANIFEST_ENTRY = "fintrack.db", "keyfile.json", "manifest.json"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024  # multipart framing + the password field
MAX_PASSWORD_BYTES = 4096
# Uncompressed caps per entry (zip-bomb guard); the DB is stored, not deflated.
ENTRY_CAPS = {DB_ENTRY: MAX_UPLOAD_BYTES, KEYFILE_ENTRY: 64 * 1024, MANIFEST_ENTRY: 64 * 1024}
SQLITE_PLAINTEXT_HEADER = b"SQLite format 3\x00"
CHUNK = 1024 * 1024


class TooLarge(Exception):
    pass


def staging_dir(vault: Vault, prefix: str) -> Path:
    """A private scratch folder inside data/ (same volume, so installs are atomic renames)."""
    data_dir = vault.settings.data_dir
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        restrict_to_owner(data_dir)
    path = data_dir / f".{prefix}-{secrets.token_hex(8)}"
    path.mkdir()
    return path


def remove_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def is_plaintext_sqlite(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(SQLITE_PLAINTEXT_HEADER)) == SQLITE_PLAINTEXT_HEADER


# ------------------------------------------------------------------ create


def recovery_sheet_of(keyfile_bytes: bytes, key: bytes | bytearray) -> str | None:
    """The number of the recovery sheet in a keyfile, if its block is intact under ``key``."""
    try:
        data = json.loads(keyfile_bytes.decode("utf-8"))
        block = RecoveryBlock.from_json(data["recovery"])
    except Exception:  # noqa: BLE001 - no block, or a malformed one: no sheet
        return None
    return block.sheet if block.mac_ok(key) else None


class BackupRaced(VaultError):
    status, code = 409, "backup_retry"
    detail = "Your password or recovery sheet changed while the backup was being made. Please try again."


BACKUP_ATTEMPTS = 2


def build_backup(vault: Vault) -> bytes:
    """The database copy and the keyfile must match: a new sheet's Done or a password change
    re-encrypts the vault and replaces keyfile.json. The vault lock isn't taken here (the
    automatic backup holds its own lock and is also started by the vault's before-lock hook,
    so taking it would invert the lock order); instead keyfile.json is read before and after
    the copy, and a copy made while it changed is thrown away and made again, once."""
    staging = staging_dir(vault, "backup")
    try:
        copy = staging / DB_ENTRY
        for _ in range(BACKUP_ATTEMPTS):
            copy.unlink(missing_ok=True)
            keyfile_before = vault.keyfile.read_bytes()
            version = vault.db.backup_to(copy)
            key = vault.db.key
            # Belt and braces: never ship a plaintext copy, and make sure it opens with the key.
            if is_plaintext_sqlite(copy) or key is None or not dbmod.verify_key(copy, key):
                raise RuntimeError("backup copy failed verification")
            keyfile_bytes = vault.keyfile.read_bytes()
            if keyfile_bytes == keyfile_before:
                break
        else:
            raise BackupRaced()
        manifest = {
            "format": FORMAT, "created_at": iso_utc(utcnow()), "schema_version": version,
            # Not secret: which recovery sheet opens this backup (its block is in the keyfile).
            "recovery_sheet": recovery_sheet_of(keyfile_bytes, key),
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(copy, DB_ENTRY, compress_type=zipfile.ZIP_STORED)  # encrypted: incompressible
            zf.writestr(KEYFILE_ENTRY, keyfile_bytes, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr(MANIFEST_ENTRY, json.dumps(manifest), compress_type=zipfile.ZIP_DEFLATED)
        return buf.getvalue()
    finally:
        remove_dir(staging)


# ------------------------------------------------------------------ upload


TEXT_FIELDS = ("password", "current_password")


class UploadError(Exception):
    """A malformed or unexpected multipart upload. ``kind`` says which (never echoes input):
    ``not_multipart``, ``unexpected_field``, ``text_too_long``, ``malformed``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass
class ReceivedFile:
    """What ``receive_file`` read: which fields came, the file part's size and its name as
    the browser sent it (untrusted: only ever reduced to a leaf name), and the text fields.
    The caller wipes ``texts`` (they may hold passwords)."""

    seen: set[str]
    size: int
    filename: str | None
    texts: dict[str, bytearray]

    def wipe(self) -> None:
        for field in self.texts.values():
            field[:] = b"\x00" * len(field)


async def receive_file(
    request: Request,
    dest: Path,
    cap: int,
    text_fields: tuple[str, ...] = (),
    *,
    max_request: int | None = None,
    max_text_bytes: int = MAX_PASSWORD_BYTES,
) -> ReceivedFile:
    """Stream a multipart/form-data body: the one ``file`` part into ``dest`` (created
    new), plus the small ``text_fields``; any other field, or a repeated one, is refused.

    Parsed incrementally with hard caps (``cap`` for the file, ``max_request`` for the whole
    body, default ``cap`` + 1 MB), so an oversize upload is refused (TooLarge) without being
    buffered in memory or spooled to the system temp folder. Malformed framing raises
    UploadError. Shared by the backup restore and the update file upload.
    """
    max_request = cap + 1024 * 1024 if max_request is None else max_request
    media, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if media != b"multipart/form-data" or not boundary:
        raise UploadError("not_multipart")

    state: dict = {"field": b"", "value": b"", "headers": {}, "name": None}
    seen: set[str] = set()
    texts = {name: bytearray() for name in text_fields}
    filename: list[str | None] = [None]
    written = 0
    fh = open(dest, "xb")

    def on_part_begin() -> None:
        state["headers"], state["name"] = {}, None

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][bytes(state["field"]).lower()] = bytes(state["value"])
        state["field"], state["value"] = b"", b""

    def on_headers_finished() -> None:
        _, opts = parse_options_header(state["headers"].get(b"content-disposition", b""))
        name = opts.get(b"name", b"").decode("latin-1")
        if name not in ("file", *text_fields) or name in seen:
            raise UploadError("unexpected_field")
        seen.add(name)
        state["name"] = name
        if name == "file":
            raw = opts.get(b"filename")
            filename[0] = raw.decode("utf-8", "replace") if raw else None

    def on_part_data(data: bytes, start: int, end: int) -> None:
        nonlocal written
        chunk = data[start:end]
        if state["name"] == "file":
            written += len(chunk)
            if written > cap:
                raise TooLarge()
            fh.write(chunk)
        elif state["name"] in texts:
            field = texts[state["name"]]
            if len(field) + len(chunk) > max_text_bytes:
                raise UploadError("text_too_long")
            field.extend(chunk)

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
        },
    )
    total = 0
    received = ReceivedFile(seen, 0, None, texts)
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_request:
                raise TooLarge()
            parser.write(chunk)
        parser.finalize()
    except (UploadError, TooLarge):
        received.wipe()
        raise
    except Exception:  # noqa: BLE001 - malformed multipart framing
        received.wipe()
        raise UploadError("malformed") from None
    finally:
        fh.close()
    received.size = written
    received.filename = filename[0]
    return received


_UPLOAD_MESSAGES = {
    "not_multipart": "Upload the backup as multipart/form-data.",
    "unexpected_field": "Unexpected form field in the upload.",
    "text_too_long": "Password is too long.",
    "malformed": "The upload is not valid multipart/form-data.",
}


async def receive_upload(request: Request, dest: Path) -> tuple[str, str | None]:
    """Stream a restore upload: the ``file`` part to ``dest``.

    Returns ``(password, current_password)``: the backup's password, and the current
    vault's password (the route requires it whenever a vault already exists).
    """
    try:
        received = await receive_file(
            request, dest, MAX_UPLOAD_BYTES, TEXT_FIELDS, max_request=MAX_REQUEST_BYTES,
        )
    except UploadError as exc:
        raise BadBackup(_UPLOAD_MESSAGES[exc.kind]) from None
    try:
        if not {"file", "password"} <= received.seen:
            raise BadBackup("Send both the backup file and the password.")
        try:
            password = received.texts["password"].decode("utf-8")
            current = (
                received.texts["current_password"].decode("utf-8")
                if "current_password" in received.seen else None
            )
        except UnicodeDecodeError:
            raise BadBackup("Password is not valid UTF-8.") from None
        return password, current
    finally:
        received.wipe()


# ------------------------------------------------------------------ validate

# Three entries with short names need a few hundred bytes of central directory.
MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024


def _check_central_directory(zip_path: Path) -> None:
    """Refuse a zip whose central directory is larger than three entries could need.

    ``zipfile.ZipFile`` builds a ZipInfo object for every central-directory record up
    front, so a 200 MB upload made only of records (millions of entries) would cost
    gigabytes of memory and minutes of CPU before any entry name is checked. Read the
    end-of-central-directory record with the same function ZipFile itself uses (so both
    agree on which record counts, including Zip64) and bound it first.
    """
    with open(zip_path, "rb") as fh:
        endrec = zipfile._EndRecData(fh)  # noqa: SLF001 - exactly what ZipFile.__init__ reads
    if not endrec:
        raise BadBackup()
    if (
        endrec[zipfile._ECD_SIZE] > MAX_CENTRAL_DIRECTORY_BYTES  # noqa: SLF001
        or endrec[zipfile._ECD_ENTRIES_TOTAL] > len(ENTRY_CAPS)  # noqa: SLF001
    ):
        raise BadBackup("The backup contains an unexpected file.")


def extract_backup(zip_path: Path, staging: Path) -> tuple[Path, Path]:
    """Validate an uploaded backup and extract it under fixed names; raises BadBackup.

    Entry names are only ever compared against the three expected names and never used
    as paths, so traversal ("../x", absolute or drive paths) is impossible by
    construction; any unexpected entry rejects the whole file.
    """
    try:
        _check_central_directory(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise BadBackup("The backup contains duplicate entries.")
            for info in infos:
                if info.filename not in ENTRY_CAPS or info.is_dir() or info.flag_bits & 0x1:
                    raise BadBackup("The backup contains an unexpected file.")
            if set(names) != set(ENTRY_CAPS):
                raise BadBackup("The backup is incomplete.")
            out = {}
            for name, cap in ENTRY_CAPS.items():
                target = staging / f"restore-{name}"
                size = 0
                with zf.open(name) as src, open(target, "wb") as dst:
                    while block := src.read(CHUNK):
                        size += len(block)
                        if size > cap:
                            raise BadBackup("The backup is too large.")
                        dst.write(block)
                out[name] = target
    except BadBackup:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, OSError, EOFError, ValueError,
            NotImplementedError, RuntimeError):
        raise BadBackup() from None

    try:
        manifest = json.loads(out[MANIFEST_ENTRY].read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BadBackup("The backup manifest is unreadable.") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BadBackup("Unsupported backup format.")
    version = manifest.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise BadBackup("The backup manifest is invalid.")
    if version > LATEST:
        raise BadBackup("This backup was made by a newer version of Iron Owl.")
    if read_keyfile(out[KEYFILE_ENTRY]) is None:
        raise BadBackup("The backup's keyfile is missing or invalid.")
    if is_plaintext_sqlite(out[DB_ENTRY]):
        raise BadBackup("The backup database is not encrypted.")
    return out[DB_ENTRY], out[KEYFILE_ENTRY]


# ------------------------------------------------------------------ previous vaults

# Restore moves the replaced vault to data/pre-restore-<YYYYmmdd-HHMMSS>[-n]/. Each copy
# still opens with the password it had, so the user can list and delete them.
PREVIOUS_RE = re.compile(r"^pre-restore-(\d{8}-\d{6})(?:-\d+)?$")
# Also the updater's scratch folders: data/.update-*, and data/updates/.incoming-* (copies of
# update files being checked), which cleanup_staging removes from data/updates/ too.
STAGING_RE = re.compile(r"^\.(?:restore|backup|pre-migrate-tmp|update|incoming)-[0-9a-f]{16}$")


def list_previous(data_dir: Path) -> list[dict]:
    if not data_dir.is_dir():
        return []
    out = []
    for path in data_dir.iterdir():
        m = PREVIOUS_RE.match(path.name)
        if not m or path.is_symlink() or not path.is_dir():
            continue
        created = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        size = sum(f.stat().st_size for f in path.iterdir() if f.is_file() and not f.is_symlink())
        out.append({"id": path.name, "created_at": created.isoformat(timespec="seconds"), "size_bytes": size})
    return sorted(out, key=lambda p: p["id"], reverse=True)


def delete_previous(data_dir: Path, previous_id: str) -> bool:
    """Delete one pre-restore copy. The id must name a direct child folder: no traversal."""
    if not PREVIOUS_RE.match(previous_id):
        return False
    path = data_dir / previous_id
    if path.is_symlink() or not path.is_dir() or path.parent.resolve() != data_dir.resolve():
        return False
    shutil.rmtree(path)
    return True


def cleanup_staging(data_dir: Path) -> None:
    """Remove scratch folders left behind by a crash mid backup/restore (run at startup)."""
    if not data_dir.is_dir():
        return
    updates = data_dir / "updates"
    folders = [data_dir]
    if updates.is_dir() and not updates.is_symlink():
        folders.append(updates)
    for folder in folders:
        for path in folder.iterdir():
            if STAGING_RE.match(path.name) and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
