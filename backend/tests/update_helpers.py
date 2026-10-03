"""Build real, signed (with an ephemeral test key) or deliberately broken v2 update files."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.update_keys import TEST_KEY_PREFIX
from app.updates import package as pkg
from app.updates.requirements import running_python

DEPS_ID = "0123456789abcdef"
CURRENT = "1.4.0"
FIXED = (1980, 1, 1, 0, 0, 0)


class Signer:
    def __init__(self, key_id: str = f"{TEST_KEY_PREFIX}key1") -> None:
        self.key = Ed25519PrivateKey.generate()
        self.key_id = key_id
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_b64 = base64.b64encode(raw).decode()

    @property
    def trusted(self) -> dict[str, str]:
        return {self.key_id: self.public_b64}

    def sig_bytes(self, manifest_bytes: bytes, *, key_id: str | None = None, domain: bytes = pkg.SIGNATURE_DOMAIN) -> bytes:
        sig = self.key.sign(domain + manifest_bytes)
        return pkg.canonical_json({"alg": "ed25519", "key_id": key_id or self.key_id,
                                   "sig": base64.b64encode(sig).decode()})


def zinfo(name: str, compress: int = zipfile.ZIP_DEFLATED, mode: int = 0o100644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = compress
    return info


def zip_bytes(entries, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    """entries: (name | ZipInfo, bytes) pairs, written in order."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            info = name if isinstance(name, zipfile.ZipInfo) else zinfo(name, compress)
            zf.writestr(info, data, compress_type=info.compress_type)
    return buf.getvalue()


def release_bytes(version: str = "1.5.0", *, schema_version: int = 5, deps_id: str = DEPS_ID,
                  kind: str = "app", python: str | None = None, bootstrap: int = 1, **extra) -> bytes:
    data = {"version": version, "schema_version": schema_version, "deps_id": deps_id, "kind": kind,
            "python": python or running_python(), "bootstrap_version": bootstrap, **extra}
    return json.dumps(data).encode()


def app_entries(version: str = "1.5.0", *, kind: str = "app", release: bytes | None = None) -> list[tuple[str, bytes]]:
    return [
        ("release.json", release if release is not None else release_bytes(version, kind=kind)),
        ("run_packaged.py", b"print('run')\n"),
        ("backend/app/__init__.py", b""),
        ("backend/app/main.py", b"VALUE = 1\n"),
        ("backend/app/routers/update.py", b"X = 2\n"),
        ("web/index.html", b"<!doctype html><title>FinTrack</title><script type=\"module\" src=\"/assets/index-Ab12_x.js\"></script>"),
        ("web/assets/index-Ab12_x.js", b"console.log('hi');" * 20),
        ("web/favicon.ico", b"\x00\x00\x01\x00" * 10),
    ]


def manifest_dict(payload: bytes, *, version: str = "1.5.0", files: int | None = None,
                  unpacked: int | None = None, **overrides) -> dict:
    if files is None or unpacked is None:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            infos = zf.infolist()
        files = len(infos) if files is None else files
        unpacked = sum(i.file_size for i in infos) if unpacked is None else unpacked
    encoded = pkg.encode_payload(payload)
    m = {
        "schema": 2, "app": "fintrack", "version": version, "released_at": "2026-09-28T12:00:00Z",
        "min_current_version": "1.0.0", "requires_reinstall": False, "kind": "app",
        "python": running_python(), "bootstrap_version": 1, "schema_version": 5, "deps_id": DEPS_ID,
        "notes": ["A new home screen shows what needs you first.", "Budget uses simpler words."],
        "payload": {"size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
                    "files": files, "unpacked_size": unpacked},
    }
    for key, value in overrides.items():
        if key == "payload_over":
            m["payload"].update(value)
        else:
            m[key] = value
    return m


def encode(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def bind_payload(manifest: dict, payload: bytes) -> dict:
    """Point the manifest at ``payload`` (plain payload.zip bytes, or anything): the
    size and SHA-256 of its ENCODED form, as the builder does."""
    encoded = pkg.encode_payload(payload)
    manifest["payload"]["size"] = len(encoded)
    manifest["payload"]["sha256"] = hashlib.sha256(encoded).hexdigest()
    return manifest


def container(manifest_bytes: bytes, sig: bytes, encoded: bytes, *, magic: bytes = pkg.MAGIC,
              lengths: tuple[int, int, int] | None = None) -> bytes:
    """A v2 container, optionally with forged magic / length fields."""
    lengths = lengths or (len(manifest_bytes), len(sig), len(encoded))
    out = bytearray(magic)
    for n, section in zip(lengths, (manifest_bytes, sig, encoded)):
        out += struct.pack("<I", n) + section
    return bytes(out)


def write_package(
    path: Path,
    signer: Signer,
    *,
    version: str = "1.5.0",
    kind: str = "app",
    entries: list[tuple[str, bytes]] | None = None,
    payload: bytes | None = None,
    encoded: bytes | None = None,
    manifest: dict | None = None,
    manifest_bytes: bytes | None = None,
    sig: bytes | None = None,
    raw: bytes | None = None,
    **manifest_overrides,
) -> Path:
    """``payload`` is the plain payload.zip (encoded here); ``encoded`` overrides the stored
    section as-is; ``raw`` writes exactly those bytes."""
    path = Path(path)
    if raw is not None:
        path.write_bytes(raw)
        return path
    if payload is None:
        payload = zip_bytes(entries if entries is not None else app_entries(version, kind=kind))
    if manifest_bytes is None:
        manifest = manifest or manifest_dict(payload, version=version, kind=kind, **manifest_overrides)
        manifest_bytes = encode(manifest)
    if sig is None:
        sig = signer.sig_bytes(manifest_bytes)
    if encoded is None:
        encoded = pkg.encode_payload(payload)
    path.write_bytes(container(manifest_bytes, sig, encoded))
    return path


def package_bytes(signer: Signer, **kw) -> bytes:
    """The bytes of a valid package (same arguments as write_package)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return write_package(Path(tmp) / "p.ftupdate", signer, **kw).read_bytes()


def sections(data: bytes) -> tuple[bytes, bytes, bytes]:
    """Split a well-formed container into (manifest, signature, encoded payload)."""
    assert data[:len(pkg.MAGIC)] == pkg.MAGIC
    pos, out = len(pkg.MAGIC), []
    for _ in range(3):
        (n,) = struct.unpack_from("<I", data, pos)
        out.append(data[pos + 4: pos + 4 + n])
        pos += 4 + n
    assert pos == len(data)
    return out[0], out[1], out[2]


def set_flag_bits(data: bytes, name: str, bits: int) -> bytes:
    """Set general-purpose flag bits for one entry in both its local and central header
    (zipfile's writer always resets them, so e.g. "encrypted" has to be patched in)."""
    out = bytearray(data)
    raw = name.encode()
    for sig, flag_at, len_at, name_at in ((b"PK\x03\x04", 6, 26, 30), (b"PK\x01\x02", 8, 28, 46)):
        pos = out.find(sig)
        while pos != -1:
            (n,) = struct.unpack_from("<H", out, pos + len_at)
            if bytes(out[pos + name_at: pos + name_at + n]) == raw:
                (flags,) = struct.unpack_from("<H", out, pos + flag_at)
                struct.pack_into("<H", out, pos + flag_at, flags | bits)
            pos = out.find(sig, pos + 4)
    return bytes(out)


# ------------------------------------------------------------------ an installed copy (API tests)

INSTALLED_AT = "2026-09-01T10:00:00Z"


def make_install_root(
    tmp_path: Path,
    *,
    current: str = CURRENT,
    previous: str | None = None,
    versions: tuple[str, ...] = (),
    runtime: bool = True,
    schema_version: int | None = None,
    **state_fields,
) -> Path:
    """A fake install root: ``state.json`` (launcher contract), ``versions/<v>/`` folders
    with ``run_packaged.py`` + ``release.json``, and ``runtimes/<DEPS_ID>/site-packages``."""
    from app.migrations import LATEST

    root = tmp_path / "FinTrack"
    for version in {current, *versions, *([previous] if previous else [])}:
        vdir = root / "versions" / version
        (vdir / "backend").mkdir(parents=True, exist_ok=True)
        (vdir / "run_packaged.py").write_text("# stub", encoding="utf-8")
        (vdir / "release.json").write_text(json.dumps({
            "version": version, "schema_version": LATEST if schema_version is None else schema_version,
            "deps_id": DEPS_ID, "kind": "app", "python": running_python(), "bootstrap_version": 1,
        }), encoding="utf-8")
    if runtime:
        (root / "runtimes" / DEPS_ID / "site-packages").mkdir(parents=True, exist_ok=True)
    state = {
        "schema": 1, "bootstrap_version": 1, "current": current, "previous": previous,
        "pending": None, "rollback": None, "port": 8000, "support_contact": "Sam",
        "installed_at": INSTALLED_AT, "future_key": {"kept": True},
    }
    state.update(state_fields)
    write_root_state(root, state)
    return root


def read_root_state(root: Path) -> dict:
    return json.loads((root / "state.json").read_text(encoding="utf-8"))


def write_root_state(root: Path, state: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")


class Exits:
    """Stands in for ``app.state.request_exit``."""

    def __init__(self) -> None:
        self.codes: list[int] = []

    def __call__(self, code: int) -> None:
        self.codes.append(code)


def aged(path: Path, seconds: float = 60) -> Path:
    import os
    import time

    t = time.time() - seconds
    os.utime(path, (t, t))
    return path


def pin_app_version(monkeypatch, version: str = CURRENT) -> None:
    """The running app reports ``version`` (default CURRENT) wherever it reads ``__version__``
    (updater, health, FastAPI), so update tests don't depend on the real ``version.py``,
    which the release build raises before it runs the tests."""
    from app import main as main_mod
    from app import version as version_mod
    from app.routers import app as app_router

    for module in (version_mod, main_mod, app_router):
        monkeypatch.setattr(module, "__version__", version)


def build_app(tmp_path: Path, monkeypatch, plaid, root: Path | None, **overrides):
    """create_app for an installed copy at ``root`` (``None``: not installed), running as
    version CURRENT. The code folder is pretended to be ``root/versions/<current>/backend``;
    exits are recorded and happen at once (no delay)."""
    from app.main import create_app
    from app.updates import mode as mode_mod
    from tests.conftest import make_settings

    pin_app_version(monkeypatch)
    values: dict = {"support_contact": "Sam"}
    if root is not None:
        current = read_root_state(root)["current"]
        vdir = root / "versions" / current
        monkeypatch.setattr(mode_mod, "BACKEND_DIR", vdir / "backend")
        values.update(fintrack_install_root=root, repo_root=vdir)
    else:
        fake = tmp_path / "code"
        (fake / "backend").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mode_mod, "BACKEND_DIR", fake / "backend")
        values.update(repo_root=fake)
    values.update(overrides)
    app = create_app(make_settings(tmp_path, **values), plaid)
    exits = Exits()
    app.state.request_exit = exits
    app.state.fintrack.updater.exit_delay = 0
    return app, exits
