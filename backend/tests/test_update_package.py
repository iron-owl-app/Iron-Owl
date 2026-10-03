"""verify_package / extract_payload: v2 container, signatures, strict manifests, zip hardening, extraction."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app import update_keys
from app.updates import package as pkg
from app.updates import semver
from app.updates.requirements import Environment, runtime_dir
from tests.update_helpers import (
    CURRENT,
    DEPS_ID,
    Signer,
    app_entries,
    bind_payload,
    container,
    encode,
    manifest_dict,
    release_bytes,
    sections,
    set_flag_bits,
    write_package,
    zinfo,
    zip_bytes,
)


@pytest.fixture
def signer(monkeypatch) -> Signer:
    s = Signer()
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", s.trusted)
    return s


def verify(path, current=CURRENT, **kw):
    return pkg.verify_package(path, current, **kw)


def rejected(path, current=CURRENT, **kw) -> str:
    with pytest.raises(pkg.UpdateRejected) as exc:
        pkg.verify_package(path, current, **kw)
    assert str(exc.value) == exc.value.code  # message is only the code (safe to log)
    return exc.value.code


# ------------------------------------------------------------------ keys + semver


def test_production_keys_hold_no_test_key():
    import importlib

    real = importlib.reload(update_keys)  # the committed module, not a monkeypatched one
    assert len(real.TRUSTED_KEYS) <= real.MAX_TRUSTED_KEYS
    import re

    for key_id, value in real.TRUSTED_KEYS.items():
        assert re.fullmatch(real.KEY_ID_PATTERN, key_id), key_id
        assert not key_id.startswith(real.TEST_KEY_PREFIX)
        raw = base64.b64decode(value, validate=True)
        assert len(raw) == 32
        Ed25519PublicKey.from_public_bytes(raw)


def test_ephemeral_test_key_is_never_trusted_by_default():
    s = Signer()
    assert s.public_b64 not in update_keys.TRUSTED_KEYS.values()


@pytest.mark.parametrize("text", ["1.4.0", "0.0.0", "10.20.300"])
def test_semver_valid(text):
    assert semver.is_valid(text)


@pytest.mark.parametrize("text", ["1.4", "1.4.0-beta", "1.4.0+b1", "01.4.0", "1.4.0 ", "v1.4.0", "1000.0.0",
                                  "١.4.0", 140, None])
def test_semver_invalid(text):
    assert not semver.is_valid(text)
    with pytest.raises(ValueError):
        semver.parse(text)


def test_semver_compare_and_display():
    assert semver.compare("1.10.0", "1.9.9") == 1
    assert semver.compare("1.4.0", "1.4.0") == 0
    assert semver.compare("1.4.0", "2.0.0") == -1
    assert semver.display("1.4.0") == "1.4"
    assert semver.display("1.4.2") == "1.4.2"


# ------------------------------------------------------------------ valid


def test_valid_package_is_ready(tmp_path, signer):
    path = write_package(tmp_path / "FinTrack-1.5.0.ftupdate", signer)
    v = verify(path)
    assert v.result == "ready" and v.code is None
    assert v.can_install and v.help is None
    assert v.manifest.version == "1.5.0" and v.manifest.display_version == "1.5"
    assert v.manifest.notes[0].startswith("A new home screen")
    assert v.manifest.minutes == 1
    assert len(v.id) == 16 and v.sha256.startswith(v.id)
    assert v.size == path.stat().st_size
    assert v.key_id == signer.key_id


def test_explicit_trusted_keys_override(tmp_path, monkeypatch):
    s = Signer()
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", {})
    path = write_package(tmp_path / "a.ftupdate", s)
    assert rejected(path) == "FT-UPD-SIG"  # production default: no keys -> nothing installs
    assert verify(path, trusted_keys=s.trusted).result == "ready"


def test_current_version_must_be_valid(tmp_path, signer):
    path = write_package(tmp_path / "a.ftupdate", signer)
    with pytest.raises(ValueError):
        verify(path, current="dev")


# ------------------------------------------------------------------ signature


def _payload():
    return zip_bytes(app_entries())


def _parts(signer, *, version="1.5.0"):
    payload = _payload()
    mb = encode(manifest_dict(payload, version=version))
    return mb, signer.sig_bytes(mb), pkg.encode_payload(payload)


def _raw(tmp_path, data: bytes, name="x.ftupdate") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_unsigned_is_sig(tmp_path, signer):
    mb, _, enc = _parts(signer)
    assert rejected(_raw(tmp_path, container(mb, b"", enc))) == "FT-UPD-SIG"


def _sig_json(**fields) -> bytes:
    return pkg.canonical_json(fields)


def _good_sig_b64(signer, mb) -> str:
    return json.loads(signer.sig_bytes(mb))["sig"]


def _flip_padding_bits(b64: str) -> str:
    """Same 64 bytes, different (non-canonical) base64 spelling."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body = b64.rstrip("=")
    last = alphabet[alphabet.index(body[-1]) ^ 1]
    out = body[:-1] + last + b64[len(body):]
    assert out != b64
    assert base64.b64decode(out, validate=True) == base64.b64decode(b64, validate=True)
    return out


@pytest.mark.parametrize("case", [
    "unknown_key", "wrong_key", "tampered", "no_domain", "v1_domain", "bad_b64", "short_sig", "alg",
    "extra_field", "missing_field", "not_json", "too_big", "dup_key", "key_id_list", "empty",
    "noncanonical_b64", "noncanonical_json_spaces", "noncanonical_json_order", "b64_newline",
])
def test_signature_failures(tmp_path, signer, case):
    mb, _, enc = _parts(signer)
    good = _good_sig_b64(signer, mb)
    other = Signer("test-other")
    kid = signer.key_id
    sig = {
        "unknown_key": lambda: _sig_json(alg="ed25519", key_id="ft-9999z", sig=good),
        "wrong_key": lambda: other.sig_bytes(mb, key_id=kid),
        "tampered": lambda: signer.sig_bytes(mb),
        "no_domain": lambda: signer.sig_bytes(mb, domain=b""),
        "v1_domain": lambda: signer.sig_bytes(mb, domain=b"FinTrack update manifest v1\x00"),
        "bad_b64": lambda: _sig_json(alg="ed25519", key_id=kid, sig="!!" + good[2:]),
        "short_sig": lambda: _sig_json(alg="ed25519", key_id=kid, sig=base64.b64encode(b"x" * 63).decode()),
        "alg": lambda: _sig_json(alg="rsa", key_id=kid, sig=good),
        "extra_field": lambda: _sig_json(alg="ed25519", key_id=kid, sig=good, note="hi"),
        "missing_field": lambda: _sig_json(alg="ed25519", sig=good),
        "not_json": lambda: b"not json at all",
        "too_big": lambda: _sig_json(alg="ed25519", key_id=kid, sig=good, pad="x" * 2000),
        "dup_key": lambda: (b'{"alg":"ed25519","key_id":"%s","sig":"%s","sig":"%s"}'
                            % (kid.encode(), good.encode(), good.encode())),
        "key_id_list": lambda: _sig_json(alg="ed25519", key_id=[kid], sig=good),
        "empty": lambda: b"",
        "noncanonical_b64": lambda: _sig_json(alg="ed25519", key_id=kid, sig=_flip_padding_bits(good)),
        "noncanonical_json_spaces": lambda: json.dumps({"alg": "ed25519", "key_id": kid, "sig": good}).encode(),
        "noncanonical_json_order": lambda: (b'{"sig":"%s","alg":"ed25519","key_id":"%s"}'
                                            % (good.encode(), kid.encode())),
        "b64_newline": lambda: _sig_json(alg="ed25519", key_id=kid, sig=good[:40] + "\n" + good[40:]),
    }[case]()
    manifest_bytes = mb
    if case == "tampered":  # signed, then changed one byte
        manifest_bytes = mb.replace(b"Budget uses", b"Budget Uses")
    assert rejected(_raw(tmp_path, container(manifest_bytes, sig, enc))) == "FT-UPD-SIG"


def test_canonical_signature_is_accepted(tmp_path, signer):
    mb, sig, enc = _parts(signer)
    assert sig == pkg.canonical_json(json.loads(sig))
    assert verify(_raw(tmp_path, container(mb, sig, enc))).result == "ready"


def test_manifest_never_parsed_when_signature_fails(tmp_path, signer, monkeypatch):
    payload = _payload()
    mb = encode(manifest_dict(payload))
    path = write_package(tmp_path / "a.ftupdate", signer, manifest_bytes=mb, sig=Signer("test-x").sig_bytes(mb,
                         key_id=signer.key_id))
    parsed: list[bytes] = []
    real_strict = pkg.strict_json

    def spy_strict(raw):
        parsed.append(raw)
        return real_strict(raw)

    def spy_manifest(raw):
        raise AssertionError("manifest parsed before its signature verified")

    monkeypatch.setattr(pkg, "strict_json", spy_strict)
    monkeypatch.setattr(pkg, "_parse_manifest", spy_manifest)
    assert rejected(path) == "FT-UPD-SIG"
    assert mb not in parsed  # only the small signature section was parsed
    # and a good signature does reach the parser
    good = write_package(tmp_path / "b.ftupdate", signer, manifest_bytes=mb)
    with pytest.raises(AssertionError):
        verify(good)


def _no_zip_parsing(monkeypatch) -> list:
    calls: list = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("zip parsed")

    monkeypatch.setattr(pkg.zipfile, "ZipFile", spy)
    monkeypatch.setattr(pkg.zipfile, "_EndRecData", spy)
    return calls


def test_no_zip_parsing_before_signature_and_hash(tmp_path, signer, monkeypatch):
    mb, sig, enc = _parts(signer)
    bad_sig = _raw(tmp_path, container(mb, Signer("test-y").sig_bytes(mb, key_id=signer.key_id), enc), "a.ftupdate")
    flipped = bytearray(enc)
    flipped[10] ^= 1
    bad_hash = _raw(tmp_path, container(mb, sig, bytes(flipped)), "b.ftupdate")
    older_mb, older_sig, _ = _parts(signer, version="1.3.0")
    older = _raw(tmp_path, container(older_mb, older_sig, enc), "c.ftupdate")
    calls = _no_zip_parsing(monkeypatch)
    assert rejected(bad_sig) == "FT-UPD-SIG"
    assert rejected(bad_hash) == "FT-UPD-DMG"
    assert verify(older).result == "older"
    assert calls == []


# ------------------------------------------------------------------ manifest (NOTUPD)


def _signed_manifest_package(tmp_path, signer, manifest_bytes: bytes) -> Path:
    return write_package(tmp_path / "m.ftupdate", signer, manifest_bytes=manifest_bytes, payload=_payload())


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(app="other"),
    lambda m: m.update(schema=1),
    lambda m: m.update(schema=3),
    lambda m: m.update(schema="2"),
    lambda m: m.update(extra=1),
    lambda m: m.pop("deps_id"),
    lambda m: m.update(version="1.5"),
    lambda m: m.update(version="1.5.0-beta"),
    lambda m: m.update(min_current_version="1"),
    lambda m: m.update(notes=[]),
    lambda m: m.update(notes=["ok"] * 7),
    lambda m: m.update(notes=["x" * 161]),
    lambda m: m.update(notes=["line one\nline two"]),
    lambda m: m.update(notes=["evil ‮ txt"]),
    lambda m: m.update(notes=["   "]),
    lambda m: m.update(notes="just a string"),
    lambda m: m.update(kind="weird"),
    lambda m: m.update(bootstrap_version=True),
    lambda m: m.update(requires_reinstall=0),
    lambda m: m.update(python="3"),
    lambda m: m.update(deps_id="XYZ"),
    lambda m: m.update(released_at="yesterday"),
    lambda m: m["payload"].update(extra=1),
    lambda m: m["payload"].update(sha256="ab"),
    lambda m: m["payload"].update(files=0),
    lambda m: m["payload"].update(size=-1),
    lambda m: m["payload"].update(size=pkg.MAX_PAYLOAD_BYTES + 1),
])
def test_bad_manifest_is_notupd(tmp_path, signer, mutate):
    m = manifest_dict(_payload())
    mutate(m)
    assert rejected(_signed_manifest_package(tmp_path, signer, encode(m))) == "FT-UPD-NOTUPD"


@pytest.mark.parametrize("raw", [
    b"not json",
    b"[]",
    "﻿{}".encode(),
    '{"schema":2}'.encode("utf-16"),
    b'{"schema":2,"schema":2}',
    b'{"schema":NaN}',
    b'{"schema":2.0}',
    b'{"schema":1e400}',
])
def test_malformed_manifest_bytes_are_notupd(tmp_path, signer, raw):
    assert rejected(_signed_manifest_package(tmp_path, signer, raw)) == "FT-UPD-NOTUPD"


def test_duplicate_key_in_valid_manifest_is_notupd(tmp_path, signer):
    mb = encode(manifest_dict(_payload()))
    dup = mb[:-1] + b',"app":"fintrack"}'
    assert rejected(_signed_manifest_package(tmp_path, signer, dup)) == "FT-UPD-NOTUPD"


# ------------------------------------------------------------------ container (v2)


def test_not_a_package_is_notupd(tmp_path, signer):
    path = tmp_path / "x.ftupdate"
    for data in (os.urandom(4096), b"", b"FTUPD", b"MZ" + os.urandom(500)):
        path.write_bytes(data)
        assert rejected(path) == "FT-UPD-NOTUPD"


def test_directory_is_not_a_package(tmp_path, signer):
    with pytest.raises((pkg.UpdateRejected, OSError)):
        verify(tmp_path)


def test_zip_files_are_not_updates(tmp_path, signer):
    # any zip, including the old (never deployed) zip-based layout, is simply not an update
    path = tmp_path / "x.ftupdate"
    path.write_bytes(zip_bytes([("readme.txt", b"hello"), ("photo.jpg", b"\xff\xd8" * 100)]))
    assert rejected(path) == "FT-UPD-NOTUPD"
    mb, sig, _ = _parts(signer)
    path.write_bytes(zip_bytes([("manifest.json", mb), ("manifest.json.sig", sig), ("payload.zip", _payload())],
                               compress=zipfile.ZIP_STORED))
    assert rejected(path) == "FT-UPD-NOTUPD"


@pytest.mark.parametrize("magic", [b"FTUPD\x00\x01", b"FTUPD\x00\x03", b"ftupd\x00\x02", b"FTUPD \x02",
                                   b"PK\x03\x04FTU", b"FTUPD\x00"])
def test_wrong_magic_is_notupd(tmp_path, signer, magic):
    mb, sig, enc = _parts(signer)
    assert rejected(_raw(tmp_path, container(mb, sig, enc, magic=magic))) == "FT-UPD-NOTUPD"


@pytest.mark.parametrize("delta", [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
                                   (1, -1, 0), (0, 1, -1), (-1, 0, 1), (0, 0, 2**31), (2**31, 0, 0)])
def test_length_fields_must_add_up_exactly(tmp_path, signer, delta):
    mb, sig, enc = _parts(signer)
    lengths = tuple(max(0, n + d) for n, d in zip((len(mb), len(sig), len(enc)), delta))
    assert rejected(_raw(tmp_path, container(mb, sig, enc, lengths=lengths))) in ("FT-UPD-DMG", "FT-UPD-SIG")


@pytest.mark.parametrize("extra", [b"\x00", b"JUNK" * 10, b"PK\x05\x06" + b"\x00" * 18])
def test_trailing_data_is_damaged(tmp_path, signer, extra):
    mb, sig, enc = _parts(signer)
    assert rejected(_raw(tmp_path, container(mb, sig, enc) + extra)) == "FT-UPD-DMG"


@pytest.mark.parametrize("prefix", [b"\x00", b"MZ" + b"\x00" * 500, pkg.MAGIC])
def test_prepended_data_is_rejected(tmp_path, signer, prefix):
    mb, sig, enc = _parts(signer)
    code = rejected(_raw(tmp_path, prefix + container(mb, sig, enc)))
    assert code == ("FT-UPD-DMG" if prefix == pkg.MAGIC else "FT-UPD-NOTUPD")


def test_data_between_sections_is_damaged(tmp_path, signer):
    mb, sig, enc = _parts(signer)
    good = container(mb, sig, enc)
    for cut in (len(pkg.MAGIC) + 4 + len(mb), len(pkg.MAGIC) + 8 + len(mb) + len(sig)):
        assert rejected(_raw(tmp_path, good[:cut] + b"\x00" + good[cut:])) == "FT-UPD-DMG"


def test_oversize_and_empty_sections(tmp_path, signer):
    mb, sig, enc = _parts(signer)
    big_manifest = mb[:-1] + b"," + b" " * pkg.MAX_MANIFEST_BYTES + b"}"
    assert rejected(_raw(tmp_path, container(big_manifest, signer.sig_bytes(big_manifest), enc))) == "FT-UPD-DMG"
    assert rejected(_raw(tmp_path, container(mb, sig + b" " * pkg.MAX_SIG_BYTES, enc))) == "FT-UPD-SIG"
    assert rejected(_raw(tmp_path, container(b"", sig, enc))) == "FT-UPD-DMG"
    assert rejected(_raw(tmp_path, container(mb, b"", enc))) == "FT-UPD-SIG"
    assert rejected(_raw(tmp_path, container(mb, sig, b""))) == "FT-UPD-DMG"
    # a length field claiming 4 GB is cut off, not an allocation
    assert rejected(_raw(tmp_path, container(mb, sig, enc, lengths=(len(mb), len(sig), 0xFFFFFFFF)))) == "FT-UPD-DMG"


def test_payload_section_cap(tmp_path, signer, monkeypatch):
    path = write_package(tmp_path / "x.ftupdate", signer)
    _, _, enc = sections(path.read_bytes())
    monkeypatch.setattr(pkg, "MAX_PAYLOAD_BYTES", len(enc) - 1)
    assert rejected(path) == "FT-UPD-DMG"


def test_file_changing_between_walk_and_read_is_damaged(tmp_path, signer, monkeypatch):
    path = write_package(tmp_path / "x.ftupdate", signer)
    mb, sig, enc = sections(path.read_bytes())
    real = pkg._section_lengths

    def walk_then_change(fh, size):
        lengths = real(fh, size)
        with open(path, "r+b") as out:  # same total size, different split
            out.write(container(mb + b" ", sig[:-1], enc))
        return lengths

    monkeypatch.setattr(pkg, "_section_lengths", walk_then_change)
    assert rejected(path) == "FT-UPD-DMG"


def test_package_bytes_hide_zip_and_script_signatures(tmp_path, signer):
    data = write_package(tmp_path / "x.ftupdate", signer).read_bytes()
    assert data.startswith(pkg.MAGIC)
    for needle in (b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b".js", b"<script", b"console.log", b"web/"):
        assert needle not in data, needle
    _, _, enc = sections(data)
    plain = pkg.decode_payload(enc)
    assert plain.startswith(b"PK\x03\x04") and b".js" in plain  # so the check above means something
    with zipfile.ZipFile(io.BytesIO(plain)) as zf:
        assert b"<script" in zf.read("web/index.html")


def test_payload_encoding_is_fixed_and_reversible():
    data = os.urandom(200_000)
    enc = pkg.encode_payload(data)
    assert enc != data and pkg.decode_payload(enc) == data and pkg.encode_payload(data) == enc
    # known answer: the keystream is SHAKE-256(label || uint64le(block)), 64 KiB blocks
    ks = pkg.encode_payload(bytes(70_000))
    assert ks[:65536] == hashlib.shake_256(pkg.PAYLOAD_ENCODING_LABEL + (0).to_bytes(8, "little")).digest(65536)
    assert ks[65536:] == hashlib.shake_256(pkg.PAYLOAD_ENCODING_LABEL + (1).to_bytes(8, "little")).digest(70_000 - 65536)
    assert pkg.encode_payload(b"") == b""


def test_pack_container_layout():
    data = pkg.pack_container(b"M" * 3, b"S" * 2, b"P")
    assert data == pkg.MAGIC + b"\x03\x00\x00\x00MMM" + b"\x02\x00\x00\x00SS" + b"\x01\x00\x00\x00P"
    assert sections(data) == (b"MMM", b"SS", b"P")


def test_forged_payload_eocd_rejected_before_zipfile(tmp_path, signer, monkeypatch):
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 30000, 30000, 50 * 1024 * 1024, 0, 0)
    payload = b"\x00" * 100 + eocd
    m = bind_payload(manifest_dict(zip_bytes(app_entries())), payload)
    m["payload"]["files"] = pkg.MAX_PAYLOAD_ENTRIES
    path = write_package(tmp_path / "x.ftupdate", signer, payload=payload, manifest=m)
    real = zipfile.ZipFile
    inner_calls = []

    def spy(file, *a, **k):
        if isinstance(file, pkg._MemReader):
            inner_calls.append(file)
        return real(file, *a, **k)

    monkeypatch.setattr(pkg.zipfile, "ZipFile", spy)
    assert rejected(path) == "FT-UPD-DMG"
    assert inner_calls == []


# ------------------------------------------------------------------ OLD / SAME / BIG / DMG


def test_older_and_same_are_results_not_errors(tmp_path, signer):
    old = verify(write_package(tmp_path / "old.ftupdate", signer, version="1.3.0"))
    assert old.result == "older" and old.code == "FT-UPD-OLD" and not old.can_install
    same = verify(write_package(tmp_path / "same.ftupdate", signer, version=CURRENT))
    assert same.result == "same" and same.code == "FT-UPD-SAME" and not same.can_install
    assert old.sha256 == hashlib.sha256((tmp_path / "old.ftupdate").read_bytes()).hexdigest()
    # a pre-release current version is not a version at all
    with pytest.raises(ValueError):
        verify(tmp_path / "old.ftupdate", current="1.5.0-rc1")


def test_older_needs_valid_signature_first(tmp_path, signer):
    payload = _payload()
    mb = encode(manifest_dict(payload, version="1.3.0"))
    path = write_package(tmp_path / "x.ftupdate", signer, manifest_bytes=mb, sig=Signer("test-z").sig_bytes(mb))
    assert rejected(path) == "FT-UPD-SIG"


def test_older_and_same_need_exact_structure(tmp_path, signer):
    for version in ("1.3.0", CURRENT):
        data = write_package(tmp_path / "x.ftupdate", signer, version=version).read_bytes()
        assert rejected(_raw(tmp_path, data + b"\x00", "a.ftupdate")) == "FT-UPD-DMG"
        assert rejected(_raw(tmp_path, data[:-1], "b.ftupdate")) == "FT-UPD-DMG"


def test_older_and_same_need_the_signed_payload(tmp_path, signer):
    """older/same are only reported for a file whose payload is exactly the signed one."""
    for version in ("1.3.0", CURRENT):
        mb, sig, enc = _parts(signer, version=version)
        assert verify(_raw(tmp_path, container(mb, sig, enc), "ok.ftupdate")).result in ("older", "same")
        flipped = bytearray(enc)
        flipped[len(enc) // 2] ^= 1  # same size, different bytes
        assert rejected(_raw(tmp_path, container(mb, sig, bytes(flipped)), "a.ftupdate")) == "FT-UPD-DMG"
        # a structurally exact file whose payload section has another length
        assert rejected(_raw(tmp_path, container(mb, sig, enc + b"\x00"), "b.ftupdate")) == "FT-UPD-DMG"
        assert rejected(_raw(tmp_path, container(mb, sig, enc[:-1]), "c.ftupdate")) == "FT-UPD-DMG"


def test_too_large(tmp_path, signer, monkeypatch):
    path = write_package(tmp_path / "x.ftupdate", signer)
    monkeypatch.setattr(pkg, "MAX_PACKAGE_BYTES", path.stat().st_size - 1)
    assert rejected(path) == "FT-UPD-BIG"


def test_encoded_payload_byte_flip_is_damaged(tmp_path, signer):
    data = write_package(tmp_path / "x.ftupdate", signer).read_bytes()
    for offset in (1, 40, 200):
        flipped = bytearray(data)
        flipped[-offset] ^= 0xFF
        assert rejected(_raw(tmp_path, bytes(flipped))) == "FT-UPD-DMG"


def test_hash_covers_encoded_bytes_not_plain(tmp_path, signer):
    payload = _payload()
    m = manifest_dict(payload)
    m["payload"]["sha256"] = hashlib.sha256(payload).hexdigest()  # hash of the PLAIN zip: wrong
    assert rejected(write_package(tmp_path / "x.ftupdate", signer, payload=payload, manifest=m)) == "FT-UPD-DMG"


def test_payload_size_mismatch_is_damaged(tmp_path, signer):
    payload = _payload()
    m = manifest_dict(payload, payload_over={"size": len(payload) + 1})
    assert rejected(write_package(tmp_path / "x.ftupdate", signer, payload=payload, manifest=m)) == "FT-UPD-DMG"


def test_unencoded_payload_is_damaged(tmp_path, signer):
    # a builder that forgot to encode: the manifest binds the stored bytes, which don't decode to a zip
    payload = _payload()
    m = manifest_dict(payload)
    m["payload"]["size"], m["payload"]["sha256"] = len(payload), hashlib.sha256(payload).hexdigest()
    path = write_package(tmp_path / "x.ftupdate", signer, manifest=m, encoded=payload)
    assert rejected(path) == "FT-UPD-DMG"


@pytest.mark.parametrize("cut", [1, 22, 500])
def test_truncated_download_is_damaged(tmp_path, signer, cut):
    path = write_package(tmp_path / "x.ftupdate", signer)
    data = path.read_bytes()
    path.write_bytes(data[:-cut])
    assert rejected(path) == "FT-UPD-DMG"


# ------------------------------------------------------------------ payload contents


def _with_entries(tmp_path, signer, entries, name="p.ftupdate", **kw):
    return write_package(tmp_path / name, signer, entries=entries, **kw)


BAD_NAMES = [
    "../evil.py", "backend/app/../../evil.py", "/backend/app/x.py", "web//x.js", "web/./x.js",
    "web/C:x.js", "web/x.js.", "web/x .js ", "web/CON.js", "web/con.txt.js", "web/lpt1.css", "web/Aux.html",
    "web/COM¹.js", "web/a\x01.js", "web/a\x7f.js", "web/a<b>.js", "web/" + "a" * 200 + ".js",
    "web/a?.js", "web/a*.js", 'web/a".js', "web/a|b.js",
    "web/CONIN$.js", "web/conout$.txt", "web/com0.js", "web/LPT0.css", "web/Conin$ .js",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_unsafe_payload_names_are_damaged(tmp_path, signer, name):
    assert pkg.path_problem(name) is not None
    entries = app_entries() + [(name, b"x")]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_backslash_and_nul_names_are_damaged(tmp_path, signer):
    for raw, placeholder in ((b"web\\evil.js", b"web|evil.js"), (b"web/ev\x00l.js", b"web/ev_l.js")):
        entries = app_entries() + [(placeholder.decode(), b"x")]
        payload = zip_bytes(entries).replace(placeholder, raw)
        path = write_package(tmp_path / "x.ftupdate", signer, payload=payload)
        assert rejected(path) == "FT-UPD-DMG"


@pytest.mark.parametrize("name", [
    "backend/app/X.py", "backend/app/x.pyc", "backend/app/data.json", "backend/app/sub-dir/x.py",
    "backend/tests/test_x.py", "backend/run.py", "web/app.exe", "web/app.js.map", "web/.htaccess",
    "site-packages/evil.py", ".env", "data/fintrack.db", "evil.py", "keyfile.json", "web",
])
def test_non_whitelisted_names_are_damaged(tmp_path, signer, name):
    assert pkg.path_problem(name) is None
    entries = app_entries() + [(name, b"x")]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_whitelist_rules():
    ok = ["release.json", "run_packaged.py", "backend/app/main.py", "backend/app/routers/a_b2.py",
          "web/index.html", "web/assets/index-Ab_1.js", "web/fonts/x.woff2", "web/fonts/x.woff", "web/manifest.webmanifest"]
    for name in ok:
        assert pkg.whitelisted(name, "app"), name
    assert not pkg.whitelisted("site-packages/a/b.pyd", "app")
    assert pkg.whitelisted("site-packages/a/b.pyd", "full")


def test_case_duplicates_are_damaged(tmp_path, signer):
    entries = app_entries() + [("web/Logo.png", b"a"), ("web/logo.png", b"b")]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_symlink_entry_is_damaged(tmp_path, signer):
    entries = app_entries() + [(zinfo("web/link.js", mode=stat.S_IFLNK | 0o777), b"../../etc")]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_directory_entry_is_damaged(tmp_path, signer):
    entries = app_entries() + [(zinfo("web/assets/"), b"")]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_encrypted_and_odd_compression_entries_are_damaged(tmp_path, signer):
    payload = set_flag_bits(zip_bytes(app_entries() + [("web/enc.js", b"x")]), "web/enc.js", 0x1)
    assert rejected(write_package(tmp_path / "a.ftupdate", signer, payload=payload)) == "FT-UPD-DMG"
    bz = zinfo("web/bz.js", zipfile.ZIP_BZIP2)
    assert rejected(_with_entries(tmp_path, signer, app_entries() + [(bz, b"x" * 100)], "b.ftupdate")) == "FT-UPD-DMG"


def test_zip_bomb_ratio_is_damaged(tmp_path, signer):
    entries = app_entries() + [("web/bomb.txt", b"\x00" * (3 * 1024 * 1024))]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_declared_sizes_are_bounded(tmp_path, signer):
    payload = _payload()
    real = manifest_dict(payload)
    for over in ({"files": real["payload"]["files"] + 1}, {"unpacked_size": real["payload"]["unpacked_size"] - 1},
                 {"unpacked_size": pkg.UNPACKED_CAPS["app"] + 1}):
        m = manifest_dict(payload, payload_over=over)
        assert rejected(write_package(tmp_path / "x.ftupdate", signer, payload=payload, manifest=m)) == "FT-UPD-DMG"


def test_release_json_required(tmp_path, signer):
    entries = [e for e in app_entries() if e[0] != "release.json"]
    assert rejected(_with_entries(tmp_path, signer, entries)) == "FT-UPD-DMG"


def test_payload_not_a_zip_is_damaged(tmp_path, signer):
    payload = os.urandom(3000)
    m = bind_payload(manifest_dict(zip_bytes(app_entries())), payload)
    assert rejected(write_package(tmp_path / "x.ftupdate", signer, payload=payload, manifest=m)) == "FT-UPD-DMG"


def test_full_kind_allows_site_packages(tmp_path, signer):
    entries = app_entries(kind="full") + [("site-packages/pkg/__init__.py", b""), ("site-packages/pkg/x.pyd", b"b")]
    v = verify(_with_entries(tmp_path, signer, entries, kind="full"))
    assert v.result == "ready" and v.manifest.kind == "full" and v.manifest.minutes == 3


# ------------------------------------------------------------------ requirements -> help


def _env(tmp_path, **kw) -> Environment:
    root = tmp_path / "install"
    runtime_dir(root, DEPS_ID).mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps({"bootstrap_version": 1}))
    kw.setdefault("disk_usage", lambda p: type("U", (), {"free": 10**12})())
    return Environment(install_root=root, data_dir=tmp_path / "data", **kw)


@pytest.mark.parametrize("overrides,env_kw,reason", [
    ({"requires_reinstall": True}, {}, "reinstall"),
    ({"python": "3.12"}, {}, "python"),
    ({"bootstrap_version": 2}, {}, "launcher"),
    ({"min_current_version": "1.4.1"}, {}, "skipped_version"),
    ({"deps_id": "fedcba9876543210"}, {}, "runtime_missing"),
    ({}, {"disk_usage": lambda p: type("U", (), {"free": 1000})()}, "disk_space"),
])
def test_help_reasons(tmp_path, signer, overrides, env_kw, reason):
    path = write_package(tmp_path / "x.ftupdate", signer, **overrides)
    v = verify(path, env=_env(tmp_path, **env_kw))
    assert v.result == "ready" and not v.can_install
    assert v.help.reason == reason and v.help.code.startswith("FT-UPD-HELP-")


def test_requirements_satisfied(tmp_path, signer):
    v = verify(write_package(tmp_path / "x.ftupdate", signer), env=_env(tmp_path))
    assert v.can_install and v.help is None


def test_launcher_bootstrap_version_from_state_json(tmp_path, signer):
    env = _env(tmp_path)
    (env.install_root / "state.json").write_text(json.dumps({"bootstrap_version": 2}))
    v = verify(write_package(tmp_path / "x.ftupdate", signer, bootstrap_version=2), env=env)
    assert v.can_install


def test_disk_space_counts_database(tmp_path, signer):
    env = _env(tmp_path)
    env.data_dir.mkdir()
    (env.data_dir / "fintrack.db").write_bytes(b"x" * 5000)
    path = write_package(tmp_path / "x.ftupdate", signer)
    unpacked = verify(path).manifest.payload.unpacked_size
    env.disk_usage = lambda p: type("U", (), {"free": 3 * unpacked + 4999})()
    assert verify(path, env=env).help.reason == "disk_space"
    env.disk_usage = lambda p: type("U", (), {"free": 3 * unpacked + 5000})()
    assert verify(path, env=env).can_install


# ------------------------------------------------------------------ extraction


def _all_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def short_tmp():
    """compileall writes temp names longer than the .pyc; pytest's basetemp can be deep
    enough to pass Windows' 260-character limit, the real install root is not."""
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="ftu"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_extract_happy_path(short_tmp, signer):
    tmp_path = short_tmp
    path = write_package(tmp_path / "x.ftupdate", signer)
    versions = tmp_path / "install" / "versions"
    out = pkg.extract_payload(path, CURRENT, versions, compile_bytecode=True)
    assert out.path == versions / "1.5.0"
    assert (out.path / "backend" / "app" / "main.py").read_bytes() == b"VALUE = 1\n"
    assert (out.path / "web" / "assets" / "index-Ab12_x.js").exists()
    assert json.loads((out.path / "release.json").read_text())["version"] == "1.5.0"
    assert list((out.path / "backend" / "app").glob("__pycache__/*.pyc"))
    assert [p.name for p in versions.iterdir()] == ["1.5.0"]  # no .partial-* left
    assert all(versions / "1.5.0" in p.parents for p in _all_files(tmp_path / "install"))
    assert out.runtime is None


def test_extract_never_overwrites(tmp_path, signer):
    path = write_package(tmp_path / "x.ftupdate", signer)
    versions = tmp_path / "versions"
    (versions / "1.5.0").mkdir(parents=True)
    (versions / "1.5.0" / "keep.txt").write_text("mine")
    with pytest.raises(pkg.ExtractError):
        pkg.extract_payload(path, CURRENT, versions)
    assert (versions / "1.5.0" / "keep.txt").read_text() == "mine"
    assert sorted(p.name for p in versions.iterdir()) == ["1.5.0"]


@pytest.mark.parametrize("release", [
    release_bytes("1.6.0"),
    release_bytes(schema_version=4),
    release_bytes(deps_id="fedcba9876543210"),
    release_bytes(kind="full"),
    release_bytes(extra_field=1),
    b"{not json",
])
def test_extract_release_json_must_match(tmp_path, signer, release):
    path = write_package(tmp_path / "x.ftupdate", signer, entries=app_entries(release=release))
    assert verify(path).result == "ready"  # contents aren't read until extraction
    versions = tmp_path / "versions"
    with pytest.raises(pkg.ExtractError):
        pkg.extract_payload(path, CURRENT, versions)
    assert list(versions.iterdir()) == []  # partial folder removed


def test_extract_reverifies(tmp_path, signer):
    versions = tmp_path / "versions"
    bad = write_package(tmp_path / "bad.ftupdate", signer, entries=app_entries() + [("../evil.py", b"x")])
    with pytest.raises(pkg.UpdateRejected):
        pkg.extract_payload(bad, CURRENT, versions)
    old = write_package(tmp_path / "old.ftupdate", signer, version="1.3.0")
    with pytest.raises(pkg.NotInstallable) as exc:
        pkg.extract_payload(old, CURRENT, versions)
    assert exc.value.code == "FT-UPD-OLD"
    helpme = write_package(tmp_path / "help.ftupdate", signer, requires_reinstall=True)
    with pytest.raises(pkg.NotInstallable) as exc:
        pkg.extract_payload(helpme, CURRENT, versions)
    assert exc.value.code == "FT-UPD-HELP-REINSTALL"
    assert not versions.exists() or list(versions.iterdir()) == []
    assert not (tmp_path / "evil.py").exists()


def test_extract_full_moves_runtime(tmp_path, signer):
    entries = app_entries(kind="full") + [("site-packages/pkg/__init__.py", b"P = 1\n")]
    path = write_package(tmp_path / "x.ftupdate", signer, entries=entries, kind="full")
    runtimes = tmp_path / "install" / "runtimes"
    out = pkg.extract_payload(path, CURRENT, tmp_path / "install" / "versions", runtimes_dir=runtimes)
    assert out.runtime == runtimes / DEPS_ID / "site-packages"
    assert (out.runtime / "pkg" / "__init__.py").read_bytes() == b"P = 1\n"
    assert not (out.path / "site-packages").exists()


def test_extract_file_dir_collision_cleans_up(tmp_path, signer):
    # "web/a.js" as a file and as a folder can't both exist: the install must fail cleanly.
    entries = app_entries() + [("web/a.js", b"x"), ("web/a.js/b.js", b"y")]
    path = write_package(tmp_path / "x.ftupdate", signer, entries=entries)
    versions = tmp_path / "versions"
    with pytest.raises(pkg.ExtractError):
        pkg.extract_payload(path, CURRENT, versions)
    assert list(versions.iterdir()) == []


def test_safe_target_refuses_escape(tmp_path):
    with pytest.raises(pkg.ExtractError):
        pkg._safe_target(tmp_path / "root", "../x")
    assert pkg._safe_target(tmp_path / "root", "a/b.js") == tmp_path / "root" / "a" / "b.js"


def test_mem_reader():
    r = pkg._MemReader(bytearray(b"0123456789"))
    assert r.read(3) == b"012"
    r.seek(-2, 2)
    assert r.read(10) == b"89"
    with pytest.raises(OSError):
        r.seek(-11, 2)
    r.seek(20)
    assert r.read(5) == b""
    assert io.BufferedReader(pkg._MemReader(bytearray(b"abc"))).read() == b"abc"


def test_tampering_after_hashing_cannot_reach_extraction(short_tmp, signer, monkeypatch):
    """The file is opened and read once; rewriting it on disk after the hash check (here:
    while the requirements run) changes nothing that gets installed."""
    entries = [(zinfo(n, zipfile.ZIP_STORED), d) for n, d in app_entries()]
    good = zip_bytes(entries + [(zinfo("backend/app/evil.py", zipfile.ZIP_STORED), b"A" * 64)],
                     compress=zipfile.ZIP_STORED)
    evil = zip_bytes(entries + [(zinfo("backend/app/evil.py", zipfile.ZIP_STORED), b"B" * 64)],
                     compress=zipfile.ZIP_STORED)
    assert len(good) == len(evil)
    path = write_package(short_tmp / "t.ftupdate", signer, payload=good)
    data = path.read_bytes()
    _, _, enc = sections(data)
    offset = len(data) - len(enc)
    real = pkg.check_requirements
    rewrites = []

    def tamper(*a, **k):
        with open(path, "r+b") as fh:
            fh.seek(offset)
            fh.write(pkg.encode_payload(evil))
        rewrites.append(1)
        return real(*a, **k)

    monkeypatch.setattr(pkg, "check_requirements", tamper)
    out = pkg.extract_payload(path, CURRENT, short_tmp / "versions")
    assert rewrites == [1]
    assert (out.path / "backend" / "app" / "evil.py").read_bytes() == b"A" * 64
    # the rewritten file itself is (correctly) damaged now
    monkeypatch.setattr(pkg, "check_requirements", real)
    assert rejected(path) == "FT-UPD-DMG"


def test_extract_opens_the_package_once(short_tmp, signer, monkeypatch):
    path = write_package(short_tmp / "t.ftupdate", signer)
    opened = []
    real_open = open

    def counting_open(file, mode="r", *a, **k):
        if os.fspath(file) == os.fspath(path):
            opened.append(mode)
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", counting_open)
    pkg.extract_payload(path, CURRENT, short_tmp / "versions")
    assert opened == ["rb"]


def test_extract_uses_long_paths(signer, short_tmp):
    """The build keeps names to 140 characters, but the verifier allows 200 and the user's install
    root may be deep: files are written through long (\\\\?\\) paths, so 260 isn't a limit."""
    root = short_tmp / ("r" * 60)
    name = "web/" + "/".join(["d" * 40] * 3) + "/" + "f" * 60 + ".js"
    assert pkg.path_problem(name) is None
    path = write_package(short_tmp / "x.ftupdate", signer, entries=app_entries() + [(name, b"deep")])
    out = pkg.extract_payload(path, CURRENT, root / "versions")
    target = out.path / name
    assert len(os.path.abspath(target)) > 260
    with open(pkg.long_path(target), "rb") as fh:
        assert fh.read() == b"deep"


def test_long_path_prefix():
    import sys

    if sys.platform != "win32":
        assert pkg.long_path("/x/y.js") == "/x/y.js"
        return
    assert pkg.long_path("C:/x/y.js") == "\\\\?\\C:\\x\\y.js"
    assert pkg.long_path("\\\\?\\C:\\x") == "\\\\?\\C:\\x"
    assert pkg.long_path("\\\\server\\share\\a") == "\\\\?\\UNC\\server\\share\\a"
