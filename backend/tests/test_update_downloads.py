"""The Downloads watcher and picked files: copy-then-verify, caching, never touching the user's files."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import update_keys
from app.services import automation
from app.updates import downloads as dl
from app.updates import package as pkg
from app.updates.store import UpdateStore
from tests.update_helpers import Signer, write_package

NOW = time.time()
CURRENT = "1.4.0"


@pytest.fixture
def signer(monkeypatch) -> Signer:
    s = Signer()
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", s.trusted)
    return s


@pytest.fixture
def folder(tmp_path) -> Path:
    path = tmp_path / "Downloads"
    path.mkdir()
    return path


@pytest.fixture
def store(tmp_path) -> UpdateStore:
    return UpdateStore(tmp_path / "data")


@pytest.fixture(autouse=True)
def never_execute(monkeypatch):
    """Nothing in the watcher may start a program or open a file with its app."""
    def boom(*a, **k):
        raise AssertionError("tried to execute something")

    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def age(path: Path, seconds: float = 60) -> Path:
    os.utime(path, (NOW - seconds, NOW - seconds))
    return path


def pkg_file(folder: Path, name: str, signer: Signer, *, seconds: float = 60, **kw) -> Path:
    return age(write_package(folder / name, signer, **kw), seconds)


class CountingVerify:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, path, current, **kw):
        self.calls.append(Path(path))
        return pkg.verify_package(path, current, **kw)


def scanner(store, folder, *, verify=None, clock=None, resolver=None) -> dl.DownloadsScanner:
    return dl.DownloadsScanner(
        store, current_version=CURRENT, folder_resolver=resolver or (lambda: folder),
        verify=verify or CountingVerify(), clock=clock or Clock(NOW),
    )


def snapshot(folder: Path) -> dict:
    return {p.name: (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns) for p in folder.iterdir()}


# ------------------------------------------------------------------ choosing the offer


def test_best_version_offered_and_downloads_untouched(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer, version="1.5.0", seconds=100)
    pkg_file(folder, "FinTrack-1.6.0.ftupdate", signer, version="1.6.0", seconds=200)
    pkg_file(folder, "FinTrack-1.3.0.ftupdate", signer, version="1.3.0")
    pkg_file(folder, "FinTrack-1.4.0 (1).ftupdate", signer, version="1.4.0")
    fake = Signer("test-fake")
    pkg_file(folder, "FinTrack-9.0.0.ftupdate", fake, version="9.0.0")  # not trusted
    pkg_file(folder, "FinTrack-2.0.0.ftupdate.crdownload", signer, version="2.0.0")  # still downloading
    (folder / "notes.txt").write_text("hello")
    before = snapshot(folder)
    result = scanner(store, folder).scan("enabled")
    assert result.offer["version"] == "1.6.0" and result.offer["source"] == "downloads"
    assert result.offer["file_name"] == "FinTrack-1.6.0.ftupdate"
    assert "inbox" not in result.offer and "sha256" not in result.offer
    assert snapshot(folder) == before  # the user's files: same bytes, same times, none removed
    inbox = list(store.inbox.iterdir())
    assert [p.name for p in inbox] == [f"{result.offer['id']}.ftupdate"]
    verdicts = {k.split("|")[0]: v["verdict"] for k, v in store.seen().items()}
    assert verdicts == {
        "FinTrack-1.5.0.ftupdate": "ready", "FinTrack-1.6.0.ftupdate": "ready",
        "FinTrack-1.3.0.ftupdate": "older", "FinTrack-1.4.0 (1).ftupdate": "same",
        "FinTrack-9.0.0.ftupdate": "rejected",
    }
    # no scratch folders left behind
    assert sorted(p.name for p in store.dir.iterdir()) == ["inbox", "state.json"]


def test_same_version_tie_prefers_newest(folder, store, signer):
    old = pkg_file(folder, "a.ftupdate", signer, version="1.5.0", seconds=300)
    new = pkg_file(folder, "b.ftupdate", signer, version="1.5.0", seconds=100,
                   notes=["Different notes, different file.", "Second point."])
    result = scanner(store, folder).scan("enabled")
    assert result.offer["file_name"] == "b.ftupdate"
    assert old.exists() and new.exists()


@pytest.mark.parametrize("kind", ["crdownload", "dir", "small", "fresh", "big", "other_ext"])
def test_ignored_candidates(folder, store, signer, kind, monkeypatch):
    if kind == "crdownload":
        p = write_package(folder / "FinTrack-1.5.0.ftupdate.crdownload", signer)
        age(p)
    elif kind == "dir":
        (folder / "FinTrack-1.5.0.ftupdate").mkdir()
    elif kind == "small":
        (folder / "FinTrack-1.5.0.ftupdate").write_bytes(b"x" * 199)
        age(folder / "FinTrack-1.5.0.ftupdate")
    elif kind == "fresh":
        pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer, seconds=2)
    elif kind == "big":
        p = pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
        monkeypatch.setattr(pkg, "MAX_PACKAGE_BYTES", p.stat().st_size - 1)
    else:
        pkg_file(folder, "FinTrack-1.5.0.zip", signer)
    verify = CountingVerify()
    result = scanner(store, folder, verify=verify).scan("enabled")
    assert result.offer is None and verify.calls == []
    assert store.seen() == {}


def test_uppercase_extension_is_found(folder, store, signer):
    pkg_file(folder, "FINTRACK-1.5.0.FTUPDATE", signer)
    assert scanner(store, folder).scan("enabled").offer["version"] == "1.5.0"


def test_links_and_junctions_skipped(folder, store, signer, tmp_path):
    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    real = pkg_file(target_dir, "real.ftupdate", signer)
    made = False
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target_dir), str(folder / "junction.ftupdate"))
        made = True
    try:
        os.symlink(real, folder / "link.ftupdate")
        made = True
    except (OSError, NotImplementedError):
        pass
    if not made:
        pytest.skip("can't create links here")
    verify = CountingVerify()
    assert scanner(store, folder, verify=verify).scan("enabled").offer is None
    assert verify.calls == []


def test_offline_placeholder_skipped(folder, store, signer):
    p = pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    if sys.platform != "win32":
        pytest.skip("Windows attributes")
    import ctypes

    assert ctypes.windll.kernel32.SetFileAttributesW(str(p), dl.FILE_ATTRIBUTE_OFFLINE)
    try:
        verify = CountingVerify()
        assert scanner(store, folder, verify=verify).scan("enabled").offer is None
        assert verify.calls == []
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x80)  # FILE_ATTRIBUTE_NORMAL


@pytest.mark.parametrize("attr", [dl.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, dl.FILE_ATTRIBUTE_RECALL_ON_OPEN,
                                  dl.FILE_ATTRIBUTE_REPARSE_POINT])
def test_cloud_placeholders_never_opened(folder, store, signer, monkeypatch, attr):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    real = dl._entry_stat

    class Fake:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            if name == "st_file_attributes":
                return attr
            return getattr(self._st, name)

    monkeypatch.setattr(dl, "_entry_stat", lambda e: Fake(real(e)))
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: opened.append(f) or real_open(f, *a, **k))
    assert scanner(store, folder).scan("enabled").offer is None
    assert not any("Downloads" in str(f) for f in opened)


def test_newest_twenty_only(folder, store, signer):
    for i in range(22):
        pkg_file(folder, f"FinTrack-1.5.{i}.ftupdate", signer, version=f"1.5.{i}", seconds=1000 - i * 10)
    cands = dl.list_candidates(folder, now=NOW)
    assert len(cands) == dl.MAX_CANDIDATES
    assert cands[0].name == "FinTrack-1.5.21.ftupdate"
    assert "FinTrack-1.5.0.ftupdate" not in {c.name for c in cands}


# ------------------------------------------------------------------ caching


def test_seen_cache_avoids_rereading(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    pkg_file(folder, "bad.ftupdate", Signer("test-bad"))
    verify = CountingVerify()
    s = scanner(store, folder, verify=verify)
    first = s.scan("enabled")
    # the untrusted one is rejected on its first block, before the verifier sees a copy
    assert len(verify.calls) == 1 and first.checked == 2
    second = s.scan("enabled", force=True)
    assert len(verify.calls) == 1 and second.checked == 0
    assert second.offer == first.offer


def test_changed_file_is_checked_again(folder, store, signer):
    p = pkg_file(folder, "FinTrack.ftupdate", signer, version="1.5.0")
    verify = CountingVerify()
    s = scanner(store, folder, verify=verify)
    assert s.scan("enabled").offer["version"] == "1.5.0"
    write_package(p, signer, version="1.6.0")
    age(p, 30)
    assert s.scan("enabled", force=True).offer["version"] == "1.6.0"
    assert len(verify.calls) == 2
    assert len(list(store.inbox.iterdir())) == 1  # keeps one package


def test_original_changed_after_copy_keeps_verified_copy(folder, store, signer):
    p = pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    s = scanner(store, folder)
    offer = s.scan("enabled").offer
    inbox = store.inbox / f"{offer['id']}.ftupdate"
    original = inbox.read_bytes()
    p.write_bytes(b"PK" + os.urandom(5000))  # replaced by something else after we copied it
    age(p, 30)
    again = s.scan("enabled", force=True)
    assert again.offer["id"] == offer["id"]
    assert inbox.read_bytes() == original
    assert pkg.verify_package(inbox, CURRENT).result == "ready"


def test_installed_ids_never_offered_again(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    s = scanner(store, folder)
    offer = s.scan("enabled").offer
    store.add_history(offer_id=offer["id"], from_version="1.4.0", to_version="1.5.0", at="2026-09-28T00:00:00Z")
    assert s.scan("enabled", force=True).offer is None
    # even with the seen cache gone, the file's id is in history
    for key in list(store.seen()):
        store.forget_seen(key)
    assert s.scan("enabled", force=True).offer is None


def test_picked_offer_not_replaced_by_older_download(folder, store, signer, tmp_path):
    staged_dir = dl.new_incoming_dir(store)
    staged = write_package(staged_dir / "package.ftupdate", signer, version="1.7.0")
    check = dl.accept_picked_file(store, staged, file_name=r"C:\Users\user\Desktop\FinTrack-1.7.ftupdate",
                                  current_version=CURRENT)
    assert check["result"] == "ready" and check["offer"]["source"] == "picked"
    assert check["offer"]["file_name"] == "FinTrack-1.7.ftupdate"
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    assert scanner(store, folder).scan("enabled").offer["version"] == "1.7.0"
    pkg_file(folder, "FinTrack-1.8.0.ftupdate", signer, version="1.8.0")
    assert scanner(store, folder).scan("enabled").offer["version"] == "1.8.0"
    assert len(list(store.inbox.iterdir())) == 1


def test_seen_ready_but_inbox_lost_is_reverified(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    s = scanner(store, folder)
    offer = s.scan("enabled").offer
    store.clear_offer()
    for p in store.inbox.iterdir():
        p.unlink()
    again = s.scan("enabled", force=True).offer
    assert again["id"] == offer["id"]
    assert (store.inbox / f"{offer['id']}.ftupdate").is_file()


def test_update_installed_drops_old_offer(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    assert scanner(store, folder).scan("enabled").offer is not None
    later = dl.DownloadsScanner(store, current_version="1.5.0", folder_resolver=lambda: folder, clock=Clock(NOW))
    assert later.scan("enabled").offer is None
    assert list(store.inbox.iterdir()) == []


# ------------------------------------------------------------------ gating


@pytest.mark.parametrize("mode", ["git", "dev", "not_installed"])
def test_disabled_modes_never_resolve_or_write(tmp_path, signer, mode):
    calls = []
    s = dl.DownloadsScanner(UpdateStore(tmp_path / "data"), current_version=CURRENT,
                            folder_resolver=lambda: calls.append(1))
    assert s.scan(mode) is None and s.scan(mode, force=True) is None
    assert calls == []
    assert not (tmp_path / "data").exists()


def test_throttle(folder, store, signer):
    calls = []
    clock = Clock(NOW)

    def resolver():
        calls.append(1)
        return folder

    s = scanner(store, folder, clock=clock, resolver=resolver)
    first = s.scan("enabled")
    assert not first.cached and len(calls) == 1
    clock.t += 10
    assert s.scan("enabled").cached and len(calls) == 1
    clock.t += 21
    assert not s.scan("enabled").cached and len(calls) == 2
    assert not s.scan("enabled", force=True).cached and len(calls) == 3


def test_busy_scan_returns_cached(folder, store, signer):
    s = scanner(store, folder)
    s._lock.acquire()
    try:
        assert s.scan("enabled", force=True).cached
    finally:
        s._lock.release()


@pytest.mark.parametrize("path", [r"\\server\share\Downloads", "//server/share/Downloads", r"\\?\UNC\server\share"])
def test_network_folder_refused_without_touching_it(store, signer, monkeypatch, path):
    touched = []
    monkeypatch.setattr(os, "scandir", lambda *a: touched.append(a))
    result = scanner(store, Path(path), resolver=lambda: Path(path)).scan("enabled")
    assert result.offer is None and touched == []


def test_mapped_network_drive_refused(store, folder, signer, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("drive letters")
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    monkeypatch.setattr(automation, "drive_type", lambda root: automation.DRIVE_REMOTE)
    verify = CountingVerify()
    assert scanner(store, folder, verify=verify).scan("enabled").offer is None
    assert verify.calls == []


def test_missing_folder(store, tmp_path, signer):
    result = scanner(store, tmp_path / "nope").scan("enabled")
    assert result.offer is None


def test_known_folder_lookup():
    found = dl.known_downloads_folder()
    if sys.platform == "win32":
        assert found is not None and found.is_absolute()
    assert dl.downloads_folder() is not None


# ------------------------------------------------------------------ picked files


def test_picked_rejected_and_older(store, signer):
    staged = dl.new_incoming_dir(store) / "p.ftupdate"
    staged.write_bytes(b"not an update" * 50)
    check = dl.accept_picked_file(store, staged, file_name="FinTrack-1.5 (1).ftupdate", current_version=CURRENT)
    assert check == {"result": "rejected", "reason": "not_update", "code": "FT-UPD-NOTUPD",
                     "file_name": "FinTrack-1.5 (1).ftupdate"}
    staged = write_package(dl.new_incoming_dir(store) / "p.ftupdate", signer, version="1.2.0")
    check = dl.accept_picked_file(store, staged, file_name="FinTrack-1.2.ftupdate", current_version=CURRENT)
    assert check == {"result": "older", "code": "FT-UPD-OLD", "file_name": "FinTrack-1.2.ftupdate",
                     "file_version": "1.2", "current_version": "1.4"}
    staged = write_package(dl.new_incoming_dir(store) / "p.ftupdate", signer, version=CURRENT)
    check = dl.accept_picked_file(store, staged, file_name="x.ftupdate", current_version=CURRENT)
    assert check["result"] == "same" and check["code"] == "FT-UPD-SAME"
    assert store.offer() is None


def test_copy_capped(tmp_path):
    src = tmp_path / "a"
    src.write_bytes(b"x" * 100)
    size, sha = dl.copy_capped(src, tmp_path / "b")
    assert size == 100 and len(sha) == 64
    with pytest.raises(dl._TooLarge):
        dl.copy_capped(src, tmp_path / "c", cap=99)
    with pytest.raises(FileExistsError):
        dl.copy_capped(src, tmp_path / "b")  # "xb": never overwrites


# ------------------------------------------------------------------ not-an-update files, bad ids, errors


def test_non_package_stops_after_first_block(folder, store, signer, monkeypatch):
    big = folder / "FinTrack-1.5.0.ftupdate"
    big.write_bytes(b"PK\x03\x04" + os.urandom(3 * dl.COPY_CHUNK))
    age(big)
    copied = []
    real = dl.copy_capped

    def spy(src, dest, cap=None, **kw):
        try:
            return real(src, dest, cap, **kw)
        finally:
            copied.append(dest.stat().st_size if dest.exists() else 0)

    monkeypatch.setattr(dl, "copy_capped", spy)
    verify = CountingVerify()
    result = scanner(store, folder, verify=verify).scan("enabled")
    assert result.offer is None and verify.calls == []  # never handed to the verifier
    assert copied == [0]  # nothing was written: the first block didn't start with the magic
    (entry,) = store.seen().values()
    assert entry["verdict"] == "rejected" and entry["sha16"] is None


def _big_payload_package(folder: Path, name: str, signer: Signer, **kw) -> Path:
    """A package whose file is several copy blocks long (a stored, incompressible entry)."""
    import zipfile

    from tests.update_helpers import app_entries, zinfo, zip_bytes

    entries = app_entries() + [(zinfo("web/assets/big.png", zipfile.ZIP_STORED), os.urandom(3 * dl.COPY_CHUNK))]
    return pkg_file(folder, name, signer, payload=zip_bytes(entries), **kw)


def _copy_spy(monkeypatch) -> list[int]:
    copied: list[int] = []
    real = dl.copy_capped

    def spy(src, dest, cap=None, **kw):
        try:
            return real(src, dest, cap, **kw)
        finally:
            copied.append(dest.stat().st_size if dest.exists() else 0)

    monkeypatch.setattr(dl, "copy_capped", spy)
    return copied


def test_untrusted_signature_stops_after_first_block(folder, store, signer, monkeypatch):
    _big_payload_package(folder, "FinTrack-9.0.0.ftupdate", Signer("test-fake"), version="9.0.0")
    copied = _copy_spy(monkeypatch)
    verify = CountingVerify()
    result = scanner(store, folder, verify=verify).scan("enabled")
    assert result.offer is None and verify.calls == []
    assert copied == [0]  # the signature was checked on the first block; nothing written
    (entry,) = store.seen().values()
    assert entry["verdict"] == "rejected" and entry["sha16"] is None


def test_bad_length_walk_stops_after_first_block(folder, store, signer, monkeypatch):
    p = _big_payload_package(folder, "FinTrack-1.5.0.ftupdate", signer)
    with open(p, "ab") as fh:
        fh.write(b"\x00")  # trailing byte: the section lengths no longer fill the file
    age(p)
    copied = _copy_spy(monkeypatch)
    verify = CountingVerify()
    assert scanner(store, folder, verify=verify).scan("enabled").offer is None
    assert verify.calls == [] and copied == [0]
    (entry,) = store.seen().values()
    assert entry["verdict"] == "rejected"


def test_signed_big_package_still_copied_and_offered(folder, store, signer, monkeypatch):
    p = _big_payload_package(folder, "FinTrack-1.5.0.ftupdate", signer)
    copied = _copy_spy(monkeypatch)
    result = scanner(store, folder).scan("enabled")
    assert result.offer["version"] == "1.5.0"
    assert copied == [p.stat().st_size]


def test_check_head_matches_verify(tmp_path, signer):
    good = write_package(tmp_path / "g.ftupdate", signer).read_bytes()
    pkg.check_head(good[:dl.COPY_CHUNK], len(good))
    with pytest.raises(pkg.UpdateRejected) as exc:
        pkg.check_head(b"PK\x03\x04" + good[4:], len(good))
    assert exc.value.code == "FT-UPD-NOTUPD"
    with pytest.raises(pkg.UpdateRejected) as exc:
        pkg.check_head(good, len(good) + 1)
    assert exc.value.code == "FT-UPD-DMG"
    with pytest.raises(pkg.UpdateRejected) as exc:
        pkg.check_head(good, len(good), trusted_keys={})
    assert exc.value.code == "FT-UPD-SIG"


def test_tampered_seen_sha16_never_becomes_a_path(folder, store, signer, monkeypatch):
    p = pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    st = p.stat()
    key = store.seen_key(p.name, st.st_size, st.st_mtime_ns)
    store.mark_seen(key, sha16="a" * 16, verdict="ready", version="1.5.0", at="2026-09-28T00:00:00Z")
    state = __import__("json").loads(store.path.read_text())
    state["seen"][key]["sha16"] = r"..\..\..\evil"
    store.path.write_text(__import__("json").dumps(state))
    touched = []
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: touched.append(str(self)) or real_is_file(self))
    result = scanner(store, folder).scan("enabled")
    assert not any("evil" in t for t in touched)
    assert result.offer["version"] == "1.5.0"  # the bad entry was dropped and the file checked again
    dl_ids = {v["sha16"] for v in store.seen().values()}
    assert dl_ids == {result.offer["id"]}


def test_reverify_inbox_refuses_bad_id(store, folder):
    s = scanner(store, folder)
    cand = dl.Candidate("x.ftupdate", folder / "x.ftupdate", 300, 1)
    assert s._reverify_inbox("../../evil", cand) is None


def test_oserror_while_checking_one_file_does_not_stop_the_scan(folder, store, signer, monkeypatch):
    pkg_file(folder, "a.ftupdate", signer, version="1.5.0", seconds=100)
    pkg_file(folder, "b.ftupdate", signer, version="1.6.0", seconds=200)
    real = dl.check_staged_file
    calls = []

    def flaky(store_, staged, **kw):
        calls.append(kw["file_name"])
        if kw["file_name"] == "a.ftupdate":
            raise PermissionError("inbox locked")
        return real(store_, staged, **kw)

    monkeypatch.setattr(dl, "check_staged_file", flaky)
    s = scanner(store, folder)
    result = s.scan("enabled")
    assert sorted(calls) == ["a.ftupdate", "b.ftupdate"]
    assert result.offer["version"] == "1.6.0"
    assert [k.split("|")[0] for k in store.seen()] == ["b.ftupdate"]  # a is looked at again next time
    assert sorted(p.name for p in store.dir.iterdir()) == ["inbox", "state.json"]  # no scratch left


def test_oserror_from_the_store_does_not_stop_the_scan(folder, store, signer, monkeypatch):
    pkg_file(folder, "a.ftupdate", signer, version="1.5.0", seconds=100)
    pkg_file(folder, "b.ftupdate", signer, version="1.6.0", seconds=200)
    real = store.mark_seen

    def flaky(key, **kw):
        if key.startswith("a.ftupdate"):
            raise OSError("disk full")
        return real(key, **kw)

    monkeypatch.setattr(store, "mark_seen", flaky)
    assert scanner(store, folder).scan("enabled").offer["version"] == "1.6.0"


# ------------------------------------------------------------------ failed updates are never offered again


def test_failed_id_never_offered_again(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    s = scanner(store, folder)
    offer = s.scan("enabled").offer
    store.record_failed(offer_id=offer["id"], version="1.5.0", code="FT-UPD-03", at="2026-09-28T00:00:00Z")
    assert s.scan("enabled", force=True).offer is None
    assert {v["verdict"] for v in store.seen().values()} == {"failed"}
    # even with the seen cache gone the id is remembered
    for key in list(store.seen()):
        store.forget_seen(key)
    assert s.scan("enabled", force=True).offer is None
    assert list(store.inbox.iterdir()) == []
    # a copy of the same file under another name is the same id
    copy = folder / "FinTrack-1.5.0 (1).ftupdate"
    copy.write_bytes((folder / "FinTrack-1.5.0.ftupdate").read_bytes())
    age(copy, 30)
    assert s.scan("enabled", force=True).offer is None
    # but a newer release is
    pkg_file(folder, "FinTrack-1.6.0.ftupdate", signer, version="1.6.0")
    assert s.scan("enabled", force=True).offer["version"] == "1.6.0"


def test_failed_id_seen_as_ready_is_not_reoffered(folder, store, signer):
    pkg_file(folder, "FinTrack-1.5.0.ftupdate", signer)
    s = scanner(store, folder)
    offer = s.scan("enabled").offer
    store.clear_offer()
    state = __import__("json").loads(store.path.read_text())
    state["failed"] = [{"id": offer["id"], "version": "1.5.0", "code": "FT-UPD-05", "at": "x"}]
    store.path.write_text(__import__("json").dumps(state))  # seen still says "ready"
    assert s.scan("enabled", force=True).offer is None
    assert {v["verdict"] for v in store.seen().values()} == {"failed"}


def test_picked_failed_file_is_not_offered(store, signer):
    staged = write_package(dl.new_incoming_dir(store) / "p.ftupdate", signer)
    first = dl.accept_picked_file(store, staged, file_name="FinTrack-1.5.ftupdate", current_version=CURRENT)
    store.record_failed(offer_id=first["offer"]["id"], version="1.5.0", code="FT-UPD-02", at="x")
    staged = write_package(dl.new_incoming_dir(store) / "p.ftupdate", signer)
    again = dl.accept_picked_file(store, staged, file_name="FinTrack-1.5.ftupdate", current_version=CURRENT)
    assert again == {"result": "failed", "code": "FT-UPD-02", "file_name": "FinTrack-1.5.ftupdate",
                     "file_version": "1.5", "current_version": "1.4"}
    assert store.offer() is None
    assert not staged.exists()  # the second copy was never kept
