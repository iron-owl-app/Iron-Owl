"""Updater mode (git/dev/not_installed/enabled) and the data/updates/state.json store."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.config import REPO_ROOT
from app.services import backup as backup_service
from app.updates import mode as mode_mod
from app.updates.store import (
    MAX_SEEN,
    StaleOffer,
    UpdateStore,
    leaf_name,
    make_offer,
    parse_launcher_result,
    public_offer,
)
from tests.conftest import make_settings

TODAY = dt.date(2026, 9, 28)


# ------------------------------------------------------------------ mode


def _install(tmp_path: Path, *, state: bool = True) -> tuple[Path, Path, Path]:
    """(repo_root, backend_dir, install_root) for an installed copy of version 1.4.0."""
    root = tmp_path / "FinTrack"
    version_dir = root / "versions" / "1.4.0"
    backend = version_dir / "backend"
    backend.mkdir(parents=True)
    if state:
        (root / "state.json").write_text(json.dumps({"current": "1.4.0"}))
    return version_dir, backend, root


def test_mode_enabled_for_installed_copy(tmp_path):
    repo, backend, root = _install(tmp_path)
    assert mode_mod.updater_mode(repo_root=repo, backend_dir=backend, install_root=root, dev=False) == "enabled"


@pytest.mark.parametrize("git_kind", ["dir", "file"])
def test_mode_git_wins(tmp_path, git_kind):
    repo, backend, root = _install(tmp_path)
    if git_kind == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: D:/elsewhere/.git/worktrees/x\n")  # a worktree
    assert mode_mod.updater_mode(repo_root=repo, backend_dir=backend, install_root=root, dev=True) == "git"


def test_mode_not_installed(tmp_path):
    repo, backend, root = _install(tmp_path, state=False)
    assert mode_mod.updater_mode(repo_root=repo, backend_dir=backend, install_root=root, dev=False) == "not_installed"
    assert mode_mod.updater_mode(repo_root=repo, backend_dir=backend, install_root=None, dev=False) == "not_installed"
    repo2, backend2, root2 = _install(tmp_path / "b")
    elsewhere = tmp_path / "copied" / "backend"
    elsewhere.mkdir(parents=True)
    assert mode_mod.updater_mode(repo_root=elsewhere.parent, backend_dir=elsewhere, install_root=root2,
                                 dev=False) == "not_installed"
    assert mode_mod.updater_mode(repo_root=root2, backend_dir=root2 / "versions", install_root=root2,
                                 dev=False) == "not_installed"


def test_mode_dev(tmp_path):
    repo, backend, root = _install(tmp_path)
    assert mode_mod.updater_mode(repo_root=repo, backend_dir=backend, install_root=root, dev=True) == "dev"


def test_this_checkout_is_git(tmp_path):
    """The owner runs from a git checkout: the real code folder must never self-update."""
    settings = make_settings(tmp_path)
    assert (REPO_ROOT / ".git").exists()
    assert mode_mod.mode_for_settings(settings) == "git"


def test_mode_for_settings_injectable(tmp_path):
    repo, backend, root = _install(tmp_path)
    settings = make_settings(tmp_path, dev=False)
    assert mode_mod.mode_for_settings(settings, repo_root=repo, backend_dir=backend, install_root=root) == "enabled"
    assert mode_mod.mode_for_settings(settings, repo_root=repo, backend_dir=backend) == "not_installed"
    settings_dev = make_settings(tmp_path, dev=True)
    assert mode_mod.mode_for_settings(settings_dev, repo_root=repo, backend_dir=backend, install_root=root) == "dev"


def test_mode_for_settings_reads_the_launcher_settings(tmp_path, monkeypatch):
    """The real setting names: FINTRACK_INSTALL_ROOT (fintrack_install_root) and REPO_ROOT
    (repo_root), as the launcher / config define them."""
    repo, backend, root = _install(tmp_path)
    settings = make_settings(tmp_path, fintrack_install_root=root, repo_root=repo)
    assert mode_mod.mode_for_settings(settings, backend_dir=backend) == "enabled"
    assert mode_mod.mode_for_settings(make_settings(tmp_path, repo_root=repo), backend_dir=backend) == "not_installed"
    # a git checkout named by repo_root always wins
    (repo / ".git").mkdir()
    assert mode_mod.mode_for_settings(settings, backend_dir=backend) == "git"
    # from the environment, exactly as the launcher passes them to the server
    (repo / ".git").rmdir()
    monkeypatch.setenv("FINTRACK_INSTALL_ROOT", str(root))
    monkeypatch.setenv("REPO_ROOT", str(repo))
    assert mode_mod.mode_for_settings(make_settings(tmp_path), backend_dir=backend) == "enabled"


@pytest.mark.parametrize("raw,expected", [
    ("Sam", "Sam"), ("  Sam  M ", "Sam M"), ("", ""), (None, ""),
    ("x" * mode_mod.MAX_CONTACT_CHARS, "x" * mode_mod.MAX_CONTACT_CHARS), ("x" * (mode_mod.MAX_CONTACT_CHARS + 1), ""),
    ("Tho\x07mas", ""), (42, ""),
])
def test_support_contact(raw, expected):
    assert mode_mod.support_contact(raw) == expected


# ------------------------------------------------------------------ store basics


def _offer(oid: str = "a" * 16, version: str = "1.5.0", can_install: bool = True, **kw) -> dict:
    offer = {"id": oid, "version": version, "display_version": version, "released_at": "2026-09-28T12:00:00Z",
             "notes": ["x"], "kind": "app", "minutes": 1, "size_bytes": 1000, "source": "downloads",
             "file_name": "FinTrack-1.5.0.ftupdate", "can_install": can_install, "help": None,
             "sha256": oid * 4, "inbox": f"{oid}.ftupdate"}
    offer.update(kw)
    return offer


def test_load_missing_is_empty_and_creates_nothing(tmp_path):
    store = UpdateStore(tmp_path / "data")
    state = store.load()
    assert state["offer"] is None and state["seen"] == {} and state["history"] == []
    assert store.banner(TODAY) == {"show": False, "remind_after": None}
    assert store.offer() is None and store.last_result() is None
    assert not (tmp_path / "data").exists()  # reading never creates data/updates/


@pytest.mark.parametrize("content", ["{broken", "[]", '{"version": 99, "offer": null}', "\x00\x01"])
def test_malformed_state_reads_empty(tmp_path, content):
    store = UpdateStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.path.write_text(content)
    assert store.load()["offer"] is None


def test_atomic_writes_leave_no_temp_file(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    assert [p.name for p in store.dir.iterdir()] == ["state.json"]
    assert json.loads(store.path.read_text())["offer"]["id"] == "a" * 16


def test_public_offer_strips_private_fields():
    pub = public_offer(_offer())
    assert "sha256" not in pub and "inbox" not in pub and pub["id"] == "a" * 16
    assert public_offer(None) is None


@pytest.mark.parametrize("name,expected", [
    (r"C:\Users\user\Downloads\FinTrack-1.5.ftupdate", "FinTrack-1.5.ftupdate"),
    ("/tmp/x/FinTrack.ftupdate", "FinTrack.ftupdate"),
    ("Fin\x00Track.ftupdate", "FinTrack.ftupdate"),
])
def test_leaf_name(name, expected):
    assert leaf_name(name) == expected


# ------------------------------------------------------------------ banner / dismiss


def test_not_now_hides_until_tomorrow(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    assert store.banner(TODAY)["show"] is True
    remind = store.dismiss("a" * 16, TODAY)
    assert remind == "2026-09-29"
    assert store.banner(TODAY) == {"show": False, "remind_after": "2026-09-29"}
    assert store.banner(TODAY + dt.timedelta(days=1)) == {"show": True, "remind_after": None}


def test_dismiss_stale_offer(tmp_path):
    store = UpdateStore(tmp_path)
    with pytest.raises(StaleOffer):
        store.dismiss("a" * 16, TODAY)
    store.set_offer(_offer())
    with pytest.raises(StaleOffer):
        store.dismiss("b" * 16, TODAY)


def test_newer_offer_resets_not_now(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    store.dismiss("a" * 16, TODAY)
    store.set_offer(_offer("b" * 16, "1.6.0"))
    assert store.banner(TODAY)["show"] is True
    # re-setting the same offer keeps its dismissal
    store.dismiss("b" * 16, TODAY)
    store.set_offer(_offer("b" * 16, "1.6.0"))
    assert store.banner(TODAY)["show"] is False


def test_needs_help_and_install_running_get_no_banner(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer(can_install=False, help={"reason": "python", "code": "FT-UPD-HELP-PYTHON"}))
    assert store.banner(TODAY)["show"] is False
    store.set_offer(_offer("b" * 16))
    assert store.banner(TODAY, install_running=True)["show"] is False


def test_drop_stale_offer(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer(version="1.5.0"))
    store.drop_stale_offer("1.4.0")
    assert store.offer() is not None
    store.drop_stale_offer("1.5.0")
    assert store.offer() is None


def test_clear_offer(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    store.clear_offer("b" * 16)
    assert store.offer() is not None
    store.clear_offer("a" * 16)
    assert store.offer() is None


# ------------------------------------------------------------------ seen / history / results


def test_seen_cache_is_capped_oldest_first(tmp_path):
    store = UpdateStore(tmp_path)
    for i in range(MAX_SEEN + 5):
        store.mark_seen(f"f{i}|1|1", sha16="0" * 16, verdict="rejected", version=None, at="2026-09-28T00:00:00Z")
    seen = store.seen()
    assert len(seen) == MAX_SEEN
    assert "f0|1|1" not in seen and f"f{MAX_SEEN + 4}|1|1" in seen
    with pytest.raises(ValueError):
        store.mark_seen("x", sha16=None, verdict="maybe", version=None, at="x")
    store.forget_seen(f"f{MAX_SEEN + 4}|1|1")
    assert f"f{MAX_SEEN + 4}|1|1" not in store.seen()


def test_history_marks_installed_and_clears_offer(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    key = store.seen_key("FinTrack-1.5.0.ftupdate", 1000, 123)
    store.mark_seen(key, sha16="a" * 16, verdict="ready", version="1.5.0", at="2026-09-28T00:00:00Z")
    store.add_history(offer_id="a" * 16, from_version="1.4.0", to_version="1.5.0", at="2026-09-28T01:00:00Z")
    assert store.installed_ids() == {"a" * 16}
    assert store.offer() is None
    assert store.seen()[key]["verdict"] == "installed"
    assert store.last_update_at() == "2026-09-28T01:00:00Z"


RESULT = {"outcome": "failed", "from_version": "1.4.0", "to_version": "1.5.0", "code": "FT-UPD-03",
          "at": "2026-09-28T09:41:00Z", "backup_kept": True}


def test_ingest_launcher_result(tmp_path):
    store = UpdateStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.launcher_result_path.write_text(json.dumps(RESULT))
    assert store.ingest_launcher_result() == RESULT
    assert not store.launcher_result_path.exists()
    assert store.last_result() == RESULT
    assert store.ingest_launcher_result() is None  # nothing new
    assert store.ack_result("2026-01-01T00:00:00Z") is False
    assert store.ack_result(RESULT["at"]) is True
    assert store.last_result() is None


@pytest.mark.parametrize("bad", [
    {**RESULT, "outcome": "maybe"}, {**RESULT, "code": "FT-UPD-3"}, {**RESULT, "from_version": "1.4"},
    {**RESULT, "backup_kept": "yes"}, {**RESULT, "at": "yesterday"}, {k: v for k, v in RESULT.items() if k != "at"},
])
def test_malformed_launcher_result_is_dropped(tmp_path, bad):
    store = UpdateStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.launcher_result_path.write_text(json.dumps(bad))
    assert store.ingest_launcher_result() is None
    assert not store.launcher_result_path.exists()
    assert store.last_result() is None


def test_parse_launcher_result_limits():
    assert parse_launcher_result(b"x" * 5000) is None
    assert parse_launcher_result(b"[1]") is None
    ok = parse_launcher_result(json.dumps({**RESULT, "code": None, "outcome": "installed", "extra": 1}).encode())
    assert ok is not None and "extra" not in ok


def test_make_offer_shape(tmp_path, monkeypatch):
    from app import update_keys
    from app.updates import package as pkg
    from tests.update_helpers import Signer, write_package

    s = Signer()
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", s.trusted)
    verified = pkg.verify_package(write_package(tmp_path / "x.ftupdate", s), "1.4.0")
    offer = make_offer(verified, source="picked", file_name=r"C:\Users\user\Downloads\x.ftupdate",
                       inbox_name=f"{verified.id}.ftupdate")
    assert set(public_offer(offer)) == {
        "id", "version", "display_version", "released_at", "notes", "kind", "minutes", "size_bytes",
        "source", "file_name", "can_install", "help",
    }
    assert offer["file_name"] == "x.ftupdate" and offer["display_version"] == "1.5"


# ------------------------------------------------------------------ backup staging cleanup


def test_cleanup_staging_removes_update_scratch(tmp_path):
    data = tmp_path / "data"
    (data / "updates" / "inbox").mkdir(parents=True)
    (data / "updates" / ".incoming-0123456789abcdef").mkdir()
    (data / ".update-0123456789abcdef").mkdir()
    (data / ".incoming-0123456789abcdef").mkdir()
    (data / "updates" / ".incoming-short").mkdir()
    backup_service.cleanup_staging(data)
    assert sorted(p.name for p in data.iterdir()) == ["updates"]
    assert sorted(p.name for p in (data / "updates").iterdir()) == [".incoming-short", "inbox"]


@pytest.mark.parametrize("patch", [{"inbox": "../../evil.ftupdate"}, {"inbox": "b" * 16 + ".ftupdate"},
                                   {"id": "../x"}, {"version": "1.5"}])
def test_tampered_offer_is_ignored(tmp_path, patch):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    state = json.loads(store.path.read_text())
    state["offer"].update(patch)
    store.path.write_text(json.dumps(state))
    assert store.offer() is None and store.offer_package() is None


def test_offer_package(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    assert store.offer_package() is None  # no copy yet
    store.inbox.mkdir(parents=True)
    (store.inbox / ("a" * 16 + ".ftupdate")).write_bytes(b"x")
    assert store.offer_package() == store.inbox / ("a" * 16 + ".ftupdate")


# ------------------------------------------------------------------ ids read back from state.json


def test_tampered_ids_are_dropped_on_load(tmp_path):
    store = UpdateStore(tmp_path)
    good = "a" * 16
    store.mark_seen("ok|1|1", sha16=good, verdict="ready", version="1.5.0", at="2026-09-28T00:00:00Z")
    state = json.loads(store.path.read_text())
    state["seen"].update({
        "evil|1|1": {"sha16": r"..\..\evil", "verdict": "ready", "version": "1.5.0", "at": "x"},
        "upper|1|1": {"sha16": "A" * 16, "verdict": "ready", "version": "1.5.0", "at": "x"},
        "num|1|1": {"sha16": 12, "verdict": "ready", "version": "1.5.0", "at": "x"},
        "verdict|1|1": {"sha16": good, "verdict": "maybe", "version": "1.5.0", "at": "x"},
        "version|1|1": {"sha16": good, "verdict": "ready", "version": "1.5", "at": "x"},
        "none|1|1": {"sha16": None, "verdict": "rejected", "version": None, "at": "x"},
        "list|1|1": ["not", "a", "dict"],
    })
    state["history"] = [{"id": "../x", "to_version": "1.5.0"}, {"id": good, "to_version": "1.5.0"},
                        {"id": None, "to_version": "1.3.0"}]
    state["failed"] = [{"id": "b" * 16}, {"id": "nope"}, "junk"]
    state["dismissals"] = {good: "2026-09-29", "../x": "2026-09-29", "c" * 16: 5}
    store.path.write_text(json.dumps(state))
    loaded = store.load()
    assert set(loaded["seen"]) == {"ok|1|1", "none|1|1"}
    assert [h["id"] for h in loaded["history"]] == [good, None]
    assert store.installed_ids() == {good}
    assert store.failed_ids() == {"b" * 16}
    assert loaded["dismissals"] == {good: "2026-09-29"}


# ------------------------------------------------------------------ failed ids are never offered again


def test_record_failed_clears_offer_and_blocks_it(tmp_path):
    store = UpdateStore(tmp_path)
    offer_id = "a" * 16
    assert store.set_offer(_offer()) is True
    key = store.seen_key("FinTrack-1.5.0.ftupdate", 1000, 123)
    store.mark_seen(key, sha16=offer_id, verdict="ready", version="1.5.0", at="2026-09-28T00:00:00Z")
    store.record_failed(offer_id=offer_id, version="1.5.0", code="FT-UPD-02", at="2026-09-28T01:00:00Z")
    assert store.offer() is None and store.failed_ids() == {offer_id}
    assert store.seen()[key]["verdict"] == "failed"
    assert store.failure(offer_id)["code"] == "FT-UPD-02"
    assert store.set_offer(_offer()) is False and store.offer() is None
    # a different file (a newer release) is offered as usual
    newer = {**_offer(), "id": "b" * 16, "inbox": "b" * 16 + ".ftupdate", "version": "1.6.0"}
    assert store.set_offer(newer) is True
    with pytest.raises(ValueError):
        store.record_failed(offer_id="../x", version="1.5.0", code=None, at="x")
    with pytest.raises(ValueError):
        store.set_offer({**_offer(), "id": "../x"})


def test_failed_offer_in_state_file_is_dropped(tmp_path):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    state = json.loads(store.path.read_text())
    state["failed"] = [{"id": "a" * 16, "version": "1.5.0", "code": "FT-UPD-03", "at": "x"}]
    store.path.write_text(json.dumps(state))
    assert store.offer() is None


def test_failed_list_is_capped(tmp_path):
    from app.updates.store import MAX_FAILED

    store = UpdateStore(tmp_path)
    for i in range(MAX_FAILED + 3):
        store.record_failed(offer_id=f"{i:016x}", version="1.5.0", code="FT-UPD-03", at="x")
    assert len(store.failed()) == MAX_FAILED and f"{0:016x}" not in store.failed_ids()


@pytest.mark.parametrize("outcome", ["failed", "rolled_back"])
def test_launcher_failure_with_id_is_never_offered_again(tmp_path, outcome):
    store = UpdateStore(tmp_path)
    store.set_offer(_offer())
    store.launcher_result_path.write_text(json.dumps({**RESULT, "outcome": outcome, "id": "a" * 16}))
    result = store.ingest_launcher_result()
    assert result["id"] == "a" * 16
    assert "id" not in store.last_result()  # UpdateResult has no id
    assert store.failed_ids() == {"a" * 16} and store.offer() is None


def test_launcher_installed_result_is_not_a_failure(tmp_path):
    store = UpdateStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.launcher_result_path.write_text(json.dumps({**RESULT, "outcome": "installed", "code": None, "id": "a" * 16}))
    assert store.ingest_launcher_result() is not None
    assert store.failed_ids() == set()


@pytest.mark.parametrize("bad_id", ["../x", "A" * 16, 7, "a" * 15])
def test_launcher_result_with_bad_id_is_malformed(tmp_path, bad_id):
    assert parse_launcher_result(json.dumps({**RESULT, "id": bad_id}).encode()) is None
