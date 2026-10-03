"""Recovery sheet: code format, crypto, the vault flows, the limiter, tampering and no leaks."""
from __future__ import annotations

import base64
import json
import logging
import re

import pytest

from app import db as dbmod
from app import recovery as rec
from app.config import REPO_ROOT
from app.main import create_app
from app import security
from app.security import KeyParams, keyfile_json, pub_digest, read_keyfile_full, read_pub_rows, write_json_atomic
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient, make_settings
from tests.test_backup import download, entries_of, fresh_client, restore, zip_bytes  # noqa: F401

NEW_PASSWORD = "a brand new passphrase 77"
THIRD_PASSWORD = "yet another passphrase 99"
VECTORS = json.loads((REPO_ROOT / "shared" / "recovery-code-vectors.json").read_text(encoding="utf-8"))
VECTOR_CODE = "".join(g["group"] for g in VECTORS["groups"])
SHEET_RE = re.compile(r"^[0-9]{3}-[0-9]{3}$")


# ------------------------------------------------------------------ helpers


def setup_sheet(client, password: str = PASSWORD) -> str:
    r = client.post("/api/auth/setup", json={"password": password})
    assert r.status_code == 200, r.text
    return "".join(r.json()["recovery"]["groups"])


def check(client, code: str):
    return client.post("/api/auth/recover/check", json={"code": code})


def recover(client, code: str, new_password: str = NEW_PASSWORD):
    return client.post("/api/auth/recover", json={"code": code, "new_password": new_password})


def auth_status(client) -> dict:
    return client.get("/api/auth/status").json()


def rstatus(client) -> dict:
    r = client.get("/api/recovery")
    assert r.status_code == 200, r.text
    return r.json()


def make_sheet(client, password: str = PASSWORD) -> dict:
    r = client.post("/api/recovery", json={"current_password": password})
    assert r.status_code == 200, r.text
    return r.json()


def activate(client, pending_id: str):
    return client.post("/api/recovery/activate", json={"pending_id": pending_id})


def block_of(settings) -> rec.RecoveryBlock | None:
    return read_keyfile_full(settings.keyfile_path)[1]


def sheet_of(code: str, settings) -> str:
    block = block_of(settings)
    return rec.sheet_number(rec.public_key(block.kdf.seed(rec.data_digits(code))))


def wrong_code() -> str:
    return "".join(rec.generate_groups())


def accounts(client) -> list[str]:
    return [a["name"] for a in client.get("/api/accounts").json()]


def add_account(client, name: str) -> None:
    assert client.post("/api/accounts", json={"name": name, "category": "bank", "current_balance": 1}).status_code == 201


# ------------------------------------------------------------------ code format (pure)


def test_shared_vectors():
    for case in VECTORS["damm"]:
        assert rec.damm(case["input"]) == case["check"], case
    for g in VECTORS["groups"]:
        assert rec.group_check(g["index"], g["data"]) + "" == g["group"][-1]
        assert g["data"] + rec.group_check(g["index"], g["data"]) == g["group"]
        assert rec.group_ok(g["index"], g["group"])
    assert rec.bad_groups(VECTOR_CODE) == []
    assert rec.data_digits(VECTOR_CODE) == bytearray(b"".join(g["data"].encode() for g in VECTORS["groups"]))


def test_every_single_substitution_and_adjacent_swap_is_caught():
    for g in VECTORS["groups"]:
        i, group = g["index"], g["group"]
        for pos in range(6):
            for d in "0123456789":
                if d != group[pos]:
                    assert not rec.group_ok(i, group[:pos] + d + group[pos + 1:]), (i, pos, d)
        for pos in range(5):
            if group[pos] != group[pos + 1]:
                swapped = group[:pos] + group[pos + 1] + group[pos] + group[pos + 2:]
                assert not rec.group_ok(i, swapped), (i, pos)


def test_a_group_typed_in_the_wrong_box_is_caught():
    groups = rec.split_groups(VECTOR_CODE)
    for a in range(6):
        for b in range(6):
            if a != b and groups[a] != groups[b]:
                swapped = list(groups)
                swapped[a], swapped[b] = swapped[b], swapped[a]
                assert set(rec.bad_groups("".join(swapped))) == {a + 1, b + 1}
    typo = VECTOR_CODE[:18] + ("0" if VECTOR_CODE[18] != "0" else "1") + VECTOR_CODE[19:]
    assert rec.bad_groups(typo) == [4]


def test_generated_groups_are_valid():
    for _ in range(200):
        groups = rec.generate_groups()
        assert len(groups) == 6
        assert all(len(g) == 6 and g.isascii() and g.isdigit() for g in groups)
        assert rec.bad_groups("".join(groups)) == []


@pytest.mark.parametrize("text", [
    VECTOR_CODE,
    " ".join(rec.split_groups(VECTOR_CODE)),
    "-".join(rec.split_groups(VECTOR_CODE)),
    " - ".join(f"{g[:3]} {g[3:]}" for g in rec.split_groups(VECTOR_CODE)),
])
def test_normalize_accepts_spaces_and_dashes(text):
    assert rec.normalize(text) == VECTOR_CODE


@pytest.mark.parametrize("text", [
    VECTOR_CODE[:-1],                                      # 35 digits
    VECTOR_CODE + "1",                                     # 37 digits
    "",
    VECTOR_CODE[:10] + "O" + VECTOR_CODE[11:],             # letter O
    "٧٣٩" + VECTOR_CODE[3:],                # Arabic-Indic digits
    "７３９" + VECTOR_CODE[3:],                # fullwidth digits
    VECTOR_CODE[:6] + "\n" + VECTOR_CODE[6:],              # newline
    VECTOR_CODE[:6] + "\t" + VECTOR_CODE[6:],              # tab
    VECTOR_CODE[:6] + " " + VECTOR_CODE[6:],          # no-break space
    12345,
    None,
])
def test_normalize_rejects_everything_else(text):
    with pytest.raises(rec.CodeFormatError) as info:
        rec.normalize(text)
    assert VECTOR_CODE[:6] not in str(info.value)


# ------------------------------------------------------------------ crypto (pure)


def test_seal_unseal_and_sheet_number():
    kdf = rec.RecoveryKdf(salt=b"s" * 16, time_cost=1, memory_cost=8192, parallelism=1)
    seed = kdf.seed(rec.data_digits(VECTOR_CODE))
    pub = rec.public_key(seed)
    key, for_salt = bytes(range(32)), b"f" * 16
    sealed = rec.seal(key, pub, for_salt)
    assert len(sealed.epk) == 32 and len(sealed.nonce) == 12 and len(sealed.ct) == 48
    assert bytes(rec.unseal(seed, pub, sealed)) == key
    # Same input, same key pair (deterministic from the code).
    assert rec.public_key(kdf.seed(rec.data_digits(VECTOR_CODE))) == pub
    other = kdf.seed(rec.data_digits(wrong_code()))
    with pytest.raises(rec.Unusable):
        rec.unseal(other, pub, sealed)
    from dataclasses import replace
    with pytest.raises(rec.Unusable):  # AAD binds the keyfile salt
        rec.unseal(seed, pub, replace(sealed, for_salt=b"g" * 16))
    with pytest.raises(rec.Unusable):
        rec.unseal(seed, pub, replace(sealed, ct=bytes(48)))
    assert SHEET_RE.match(rec.sheet_number(pub))


def test_block_round_trip_and_mac():
    key = bytes(range(32))
    kdf = rec.RecoveryKdf(salt=b"s" * 16, time_cost=1, memory_cost=8192, parallelism=1)
    pub = rec.public_key(kdf.seed(rec.data_digits(VECTOR_CODE)))
    block = rec.new_block(key, pub, kdf, b"f" * 16, confirmed=False)
    parsed = rec.RecoveryBlock.from_json(json.loads(json.dumps(block.to_json())))
    assert parsed == block and parsed.mac_ok(key)
    assert not parsed.mac_ok(bytes(32)) and not parsed.mac_ok(None)
    from dataclasses import replace
    assert not replace(parsed, confirmed=True).mac_ok(key)


def test_merge_retired_is_unique_and_capped():
    def r(n):
        return rec.Retired(bytes([n]) * 32, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")

    active = bytes([99]) * 32
    merged = rec.merge_retired([r(1), r(2)], [r(2), rec.Retired(active, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")],
                               [r(n) for n in range(3, 20)], exclude=active)
    assert len(merged) == rec.MAX_RETIRED
    assert [m.public_key[0] for m in merged[:3]] == [1, 2, 3]


# ------------------------------------------------------------------ setup and status


def test_setup_returns_the_sheet_once(client, settings):
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body.keys() == {"ok", "session_token", "recovery"}
    sheet = body["recovery"]
    assert sheet.keys() == {"sheet", "created_at", "groups"}
    assert SHEET_RE.match(sheet["sheet"]) and sheet["created_at"].endswith("Z")
    assert len(sheet["groups"]) == 6 and rec.bad_groups("".join(sheet["groups"])) == []

    raw = json.loads(settings.keyfile_path.read_text())
    block = raw["recovery"]
    assert block["version"] == 1 and block["confirmed"] is False and block["kdf"] == "argon2id"
    assert (block["time_cost"], block["memory_cost"], block["parallelism"]) == (3, 65536, 4)
    assert block["sealed"]["alg"] == "x25519-hkdf-sha256-aes256gcm"
    assert block["sealed"]["for_salt"] == raw["salt"] and block["retired"] == []
    assert "sheet" not in block  # always derived from the public key
    code = "".join(sheet["groups"])
    assert code not in settings.keyfile_path.read_text()
    assert sheet_of(code, settings) == sheet["sheet"]

    assert auth_status(client)["recovery"] == {"available": True, "sheet": sheet["sheet"]}
    assert rstatus(client) == {"status": "unconfirmed", "sheet": sheet["sheet"], "created_at": sheet["created_at"]}

    r = client.post("/api/recovery/confirm", json={"sheet": "000-000" if sheet["sheet"] != "000-000" else "000-001"})
    assert r.status_code == 409 and r.json()["code"] == "sheet_changed"
    assert client.post("/api/recovery/confirm", json={"sheet": "12-3456"}).status_code == 422
    r = client.post("/api/recovery/confirm", json={"sheet": sheet["sheet"]})
    assert r.status_code == 200
    assert r.json() == {"status": "active", "sheet": sheet["sheet"], "created_at": sheet["created_at"]}
    assert rstatus(client)["status"] == "active"
    # The unconfirmed sheet already worked; confirming changes nothing else.
    client.post("/api/auth/lock")
    assert check(client, code).json() == {"ok": True, "sheet": sheet["sheet"]}


def test_status_shows_support_contact(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path, support_contact="  Sam "), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        st = auth_status(c)
    assert st["support_contact"] == "Sam"
    assert st["recovery"] == {"available": False, "sheet": None}
    assert st["retry_after"] is None and st["tries_left"] == 5


# ------------------------------------------------------------------ recover


def test_check_then_recover_sets_a_new_password(client, settings):
    code = setup_sheet(client)
    sheet = auth_status(client)["recovery"]["sheet"]
    add_account(client, "Savings")
    client.post("/api/auth/lock")

    r = check(client, " ".join(rec.split_groups(code)))
    assert r.status_code == 200 and r.json() == {"ok": True, "sheet": sheet}
    assert auth_status(client)["unlocked"] is False  # check opens nothing

    r = recover(client, "-".join(rec.split_groups(code)))
    assert r.status_code == 200, r.text
    assert r.json().keys() == {"ok", "session_token"} and "ft_session=" in r.headers["set-cookie"]
    assert accounts(client) == ["Savings"]
    assert rstatus(client)["status"] == "unconfirmed"  # same sheet, still waiting for its Continue

    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200
    assert accounts(client) == ["Savings"]
    # The same sheet keeps working, now for the new key.
    params, block = read_keyfile_full(settings.keyfile_path)
    assert block.sealed.for_salt == params.salt and block.sheet == sheet
    client.post("/api/auth/lock")
    assert recover(client, code, THIRD_PASSWORD).status_code == 200
    assert accounts(client) == ["Savings"]


def test_sheet_survives_two_password_changes(client, settings):
    code = setup_sheet(client)
    for current, new in ((PASSWORD, NEW_PASSWORD), (NEW_PASSWORD, THIRD_PASSWORD)):
        r = client.post("/api/auth/change-password", json={"current_password": current, "new_password": new})
        assert r.status_code == 200
        params, block = read_keyfile_full(settings.keyfile_path)
        assert block.sealed.for_salt == params.salt
        assert block.mac_ok(params.derive(new))
    client.post("/api/auth/lock")
    assert recover(client, code, "after two changes 123").status_code == 200


def test_recover_errors(client, settings):
    r = check(client, VECTOR_CODE)
    assert r.status_code == 409 and r.json()["code"] == "not_initialized"
    code = setup_sheet(client)
    client.post("/api/auth/lock")

    typo = code[:18] + ("0" if code[18] != "0" else "1") + code[19:]
    r = check(client, typo)
    assert r.status_code == 422 and r.json() == {"detail": "Some groups have a typo.", "code": "typo", "groups": [4]}
    r = check(client, code + "1")
    assert r.status_code == 422 and r.json() == {"detail": "Enter the 36 digits from your recovery sheet.", "code": "bad_format"}
    r = recover(client, code, "short")
    assert r.status_code == 422 and r.json()["code"] == "weak_password"
    r = recover(client, typo, "short")  # typo first: the code is checked before the password
    assert r.json()["code"] == "typo"
    # Wrong type / too long: pydantic 422 without echoing the input.
    for body in ({"code": int(code[:18])}, {"code": code * 4}, {"code": code, "extra": 1}):
        r = client.post("/api/auth/recover/check", json=body)
        assert r.status_code == 422 and code[:12] not in r.text
    assert auth_status(client)["tries_left"] == 5  # none of these counted


def test_recovering_locks_the_other_session(client, app, vault):
    code = setup_sheet(client)
    calls = []
    before, after = vault.before_lock, vault.after_unlock
    vault.before_lock = lambda: (calls.append("before_lock"), before())
    vault.after_unlock = lambda: (calls.append("after_unlock"), after())
    other = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    r = recover(other, code)
    assert r.status_code == 200
    assert client.get("/api/accounts").status_code == 401
    # the other window learns its session moved here (App-window states)
    assert client.get("/api/accounts").json() == {"detail": "moved"}
    assert other.get("/api/accounts").status_code == 200
    assert calls == ["before_lock", "after_unlock"]


# ------------------------------------------------------------------ crash safety


def _crash_after_rekey(settings, old_password: str, new_password: str) -> KeyParams:
    """What change-password or recover leaves behind if the process dies after PRAGMA rekey."""
    params, block = read_keyfile_full(settings.keyfile_path)
    old_key = params.derive(old_password)
    new_params = KeyParams.new()
    new_key = new_params.derive(new_password)
    write_json_atomic(settings.keyfile_path.with_name("keyfile.json.new"),
                      keyfile_json(new_params, block.resealed(new_key, new_params.salt)))
    dbmod.rekey(settings.db_path, old_key, new_key)
    return new_params


def test_recover_finishes_an_interrupted_rekey(client, settings):
    code = setup_sheet(client)
    add_account(client, "Brokerage")
    client.post("/api/auth/lock")
    new_params = _crash_after_rekey(settings, PASSWORD, NEW_PASSWORD)
    new_path = settings.keyfile_path.with_name("keyfile.json.new")

    assert recover(client, code, THIRD_PASSWORD).status_code == 200
    assert not new_path.exists()
    assert read_keyfile_full(settings.keyfile_path)[0].salt != new_params.salt  # rekeyed again
    assert accounts(client) == ["Brokerage"]
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 401
    assert client.post("/api/auth/unlock", json={"password": THIRD_PASSWORD}).status_code == 200


def test_password_unlock_finishes_an_interrupted_rekey_and_keeps_the_sheet(client, settings):
    code = setup_sheet(client)
    client.post("/api/auth/lock")
    new_params = _crash_after_rekey(settings, PASSWORD, NEW_PASSWORD)
    assert client.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200
    assert read_keyfile_full(settings.keyfile_path)[0].salt == new_params.salt
    client.post("/api/auth/lock")
    assert check(client, code).status_code == 200


def test_recover_discards_a_stale_new_keyfile(client, settings):
    code = setup_sheet(client)
    client.post("/api/auth/lock")
    new_path = settings.keyfile_path.with_name("keyfile.json.new")
    write_json_atomic(new_path, KeyParams.new().to_json())  # crash before the rekey
    assert recover(client, code).status_code == 200
    assert not new_path.exists()


def test_failed_rekey_during_recover_changes_nothing(client, settings, vault, monkeypatch):
    code = setup_sheet(client)
    add_account(client, "Kept")
    client.post("/api/auth/lock")
    keyfile_before = settings.keyfile_path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk went away")

    monkeypatch.setattr(dbmod, "rekey", boom)
    r = recover(client, code)
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
    assert not vault.unlocked and vault.db.key is None
    assert settings.keyfile_path.read_bytes() == keyfile_before
    assert not settings.keyfile_path.with_name("keyfile.json.new").exists()
    monkeypatch.undo()
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert accounts(client) == ["Kept"]
    client.post("/api/auth/lock")
    assert check(client, code).status_code == 200


# ------------------------------------------------------------------ a new sheet from Settings


def test_new_sheet_is_pending_until_done(client, settings):
    old = setup_sheet(client)
    old_sheet = auth_status(client)["recovery"]["sheet"]

    r = client.post("/api/recovery", json={"current_password": ""})
    assert r.status_code == 422 and r.json()["code"] == "password_required"
    r = client.post("/api/recovery", json={"current_password": "not my password"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Your current Iron Owl password is incorrect.", "code": "wrong_password", "tries_left": 4}

    p = make_sheet(client)
    assert p.keys() == {"pending_id", "sheet", "created_at", "groups"} and len(p["pending_id"]) <= 64
    new = "".join(p["groups"])
    assert rec.bad_groups(new) == [] and p["sheet"] != old_sheet
    assert new not in settings.keyfile_path.read_text()
    # Pending: nothing changed yet; the old sheet still works, the new one doesn't.
    assert rstatus(client)["sheet"] == old_sheet
    assert check(client, old).status_code == 200
    r = check(client, new)
    assert r.status_code == 401 and r.json()["code"] == "no_match" and r.json()["active_sheet"] == old_sheet

    r = activate(client, p["pending_id"])
    assert r.status_code == 200
    assert r.json() == {"status": "active", "sheet": p["sheet"], "created_at": p["created_at"]}
    assert auth_status(client)["recovery"]["sheet"] == p["sheet"]
    block = block_of(settings)
    assert len(block.retired) == 1 and rec.sheet_number(block.retired[0].public_key) == old_sheet
    assert activate(client, p["pending_id"]).json()["code"] == "pending_expired"  # used up

    tries = auth_status(client)["tries_left"]
    r = check(client, old)
    assert r.status_code == 409
    assert r.json() == {"detail": "This recovery sheet is out of date.", "code": "outdated", "active_sheet": p["sheet"]}
    assert auth_status(client)["tries_left"] == tries  # not counted
    assert check(client, new).status_code == 200
    client.post("/api/auth/lock")
    assert recover(client, new).status_code == 200


def test_cancelled_new_sheet_never_works(client):
    old = setup_sheet(client)
    p = make_sheet(client)  # the user cancels: no activate
    client.post("/api/auth/lock")
    assert check(client, "".join(p["groups"])).json()["code"] == "no_match"
    assert check(client, old).status_code == 200


def test_pending_sheet_is_dropped_by_lock(client):
    setup_sheet(client)
    p = make_sheet(client)
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    r = activate(client, p["pending_id"])
    assert r.status_code == 409 and r.json()["code"] == "pending_expired"


def test_pending_sheet_is_dropped_by_another_session(client, app):
    setup_sheet(client)
    p = make_sheet(client)
    other = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    assert other.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert activate(client, p["pending_id"]).status_code == 401  # this tab's session ended
    assert activate(other, p["pending_id"]).json()["code"] == "pending_expired"


def test_pending_sheet_expires_after_30_minutes(client, clock):
    setup_sheet(client)
    p = make_sheet(client)
    for _ in range(2):
        clock.advance(14 * 60)
        rstatus(client)  # activity, so the idle auto-lock doesn't fire first
    clock.advance(2 * 60 + 1)
    r = activate(client, p["pending_id"])
    assert r.status_code == 409 and r.json()["code"] == "pending_expired"


def test_password_change_between_new_sheet_and_done(client, settings, vault):
    setup_sheet(client)
    p = make_sheet(client)
    before = vault._pending.key
    r = client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    # The pending key came from the old password: it is wiped and derived again from the new one.
    assert before == bytearray(32) and vault._pending.key != bytearray(32)
    assert activate(client, p["pending_id"]).status_code == 200
    params, block = read_keyfile_full(settings.keyfile_path)
    assert block.sealed.for_salt == params.salt
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200
    client.post("/api/auth/lock")
    assert recover(client, "".join(p["groups"]), THIRD_PASSWORD).status_code == 200


def test_wrong_pending_id(client):
    setup_sheet(client)
    make_sheet(client)
    assert activate(client, "nope").json()["code"] == "pending_expired"
    assert client.post("/api/recovery/activate", json={"pending_id": "x" * 65}).status_code == 422


# ------------------------------------------------------------------ a new sheet revokes the old one


def offline_key(keyfile_text: str | bytes, code: str) -> bytearray:
    """What someone holding a sheet and a copy of a keyfile can get, without FinTrack."""
    block = rec.RecoveryBlock.from_json(json.loads(keyfile_text)["recovery"])
    seed = block.kdf.seed(rec.data_digits(rec.normalize(code)))
    return rec.unseal(seed, block.public_key, block.sealed)


def test_done_rotates_the_vault_key(client, settings, vault):
    setup_sheet(client)
    add_account(client, "Kept")
    params_before, _ = read_keyfile_full(settings.keyfile_path)
    key_before = bytes(vault.db.key)
    session_token = client.headers[security.SESSION_HEADER]
    p = make_sheet(client)
    assert activate(client, p["pending_id"]).status_code == 200
    params, block = read_keyfile_full(settings.keyfile_path)
    assert params.salt != params_before.salt and block.sealed.for_salt == params.salt
    assert bytes(vault.db.key) != key_before and not dbmod.verify_key(settings.db_path, key_before)
    assert client.headers[security.SESSION_HEADER] == session_token  # same session, still open
    assert accounts(client) == ["Kept"]
    assert read_pub_rows(vault.db) == (pub_digest(block.public_key), None)
    assert not settings.keyfile_path.with_name("keyfile.json.new").exists()
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200  # same password
    assert accounts(client) == ["Kept"]


def test_old_sheet_and_old_keyfile_open_nothing_current(client, settings):
    """The lost-sheet case: the old sheet + any keyfile copy from its time (backup,
    pre-migrate, pre-restore) gives the old key, which no longer opens the live vault."""
    old = setup_sheet(client)
    old_keyfile = settings.keyfile_path.read_text()
    old_backup = entries_of(download(client).content)
    p = make_sheet(client)
    assert activate(client, p["pending_id"]).status_code == 200
    later_backup = entries_of(download(client).content)
    client.post("/api/auth/lock")
    assert check(client, old).json()["code"] == "outdated"

    old_key = offline_key(old_keyfile, old)
    assert not dbmod.verify_key(settings.db_path, old_key)
    # It opens the backup made while it was the active sheet (accepted, documented)...
    assert offline_key(old_backup["keyfile.json"], old) == old_key
    backup_db = settings.data_dir / "old-backup.db"
    backup_db.write_bytes(old_backup["fintrack.db"])
    assert dbmod.verify_key(backup_db, old_key)
    # ...but not a later one: that one holds only the new sheet, sealed to the new key.
    later_db = settings.data_dir / "later-backup.db"
    later_db.write_bytes(later_backup["fintrack.db"])
    assert not dbmod.verify_key(later_db, old_key)
    with pytest.raises(rec.Unusable):
        offline_key(later_backup["keyfile.json"], old)
    new_key = offline_key(later_backup["keyfile.json"], "".join(p["groups"]))
    assert dbmod.verify_key(later_db, new_key) and dbmod.verify_key(settings.db_path, new_key)


def test_in_app_restore_rotates_the_key_too(client, settings):
    """Restoring an old backup in the app: the backup's sheet (maybe the lost one) and the
    backup's own keyfile don't open the restored vault; the current sheet does."""
    old = setup_sheet(client)
    add_account(client, "Backed up")
    backup = download(client).content
    backup_keyfile = entries_of(backup)["keyfile.json"]
    p = make_sheet(client)
    assert activate(client, p["pending_id"]).status_code == 200
    r = restore(client, backup, current_password=PASSWORD)
    assert r.status_code == 200, r.text
    assert accounts(client) == ["Backed up"]
    params, block = read_keyfile_full(settings.keyfile_path)
    assert params.salt != KeyParams.from_json(json.loads(backup_keyfile)).salt
    assert rstatus(client) == {"status": "active", "sheet": p["sheet"], "created_at": p["created_at"]}

    assert not dbmod.verify_key(settings.db_path, offline_key(backup_keyfile, old))
    assert not dbmod.verify_key(settings.db_path, KeyParams.from_json(json.loads(backup_keyfile)).derive(PASSWORD))
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200  # the backup's password
    client.post("/api/auth/lock")
    assert check(client, old).json()["code"] == "outdated"
    assert recover(client, "".join(p["groups"])).status_code == 200
    assert accounts(client) == ["Backed up"]


def test_a_rolled_back_block_is_never_revived(client, settings):
    """Write access to data/: put the old (validly MAC'd, at its time) block back."""
    old = setup_sheet(client)
    old_block = json.loads(settings.keyfile_path.read_text())["recovery"]
    p = make_sheet(client)
    assert activate(client, p["pending_id"]).status_code == 200
    data = json.loads(settings.keyfile_path.read_text())
    data["recovery"] = old_block
    settings.keyfile_path.write_text(json.dumps(data))

    assert rstatus(client)["status"] == "stale"
    r = client.post("/api/recovery/confirm", json={"sheet": rec.RecoveryBlock.from_json(old_block).sheet})
    assert r.json()["code"] == "sheet_changed"
    r = client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    params, after = read_keyfile_full(settings.keyfile_path)
    assert after.sealed.for_salt != params.salt  # not re-sealed
    client.post("/api/auth/lock")
    assert check(client, old).json()["code"] == "unusable"  # its key opens nothing
    assert check(client, "".join(p["groups"])).json()["code"] == "no_match"


def test_a_block_not_on_record_is_never_resealed(client, settings, vault):
    """Defense in depth: even a block MAC'd under the current key is trusted only if its
    public key is the one on record inside the encrypted database."""
    code = setup_sheet(client)
    params, block = read_keyfile_full(settings.keyfile_path)
    other = wrong_code()
    other_pub = rec.public_key(block.kdf.seed(rec.data_digits(other)))
    key = bytearray(vault.db.key)
    forged = rec.new_block(key, other_pub, block.kdf, params.salt, confirmed=True)
    write_json_atomic(settings.keyfile_path, keyfile_json(params, forged))
    assert forged.mac_ok(key)

    assert rstatus(client)["status"] == "stale"
    assert client.post("/api/recovery/confirm", json={"sheet": forged.sheet}).json()["code"] == "sheet_changed"
    r = client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    params, after = read_keyfile_full(settings.keyfile_path)
    assert after.public_key == other_pub and after.sealed.for_salt != params.salt
    p = make_sheet(client, NEW_PASSWORD)
    assert activate(client, p["pending_id"]).status_code == 200
    assert block_of(settings).retired == ()  # not carried over as a retired sheet either
    client.post("/api/auth/lock")
    assert check(client, other).json()["code"] == "no_match"
    assert check(client, code).json()["code"] == "no_match"
    assert recover(client, "".join(p["groups"])).status_code == 200


def test_the_pending_key_is_wiped(client, app, vault):
    setup_sheet(client)
    make_sheet(client)
    first = vault._pending.key
    make_sheet(client)  # replaced by a newer one
    assert first == bytearray(32)
    second = vault._pending.key
    other = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    assert other.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200  # a new session
    assert vault._pending is None and second == bytearray(32)
    make_sheet(other)
    third = vault._pending.key
    other.post("/api/auth/lock")
    assert vault._pending is None and third == bytearray(32)


def _crash_activate(monkeypatch, settings, vault, client, *, stage: str) -> dict:
    """Done, but the process dies ``before`` or ``after`` the PRAGMA rekey (in the second
    case, before keyfile.json.new is renamed). Returns the pending sheet."""
    p = make_sheet(client)
    new_path = settings.keyfile_path.with_name("keyfile.json.new")
    saved = settings.data_dir / "saved.new"
    if stage == "before":
        def rekey(*_a, **_k):
            saved.write_bytes(new_path.read_bytes())
            raise OSError("power cut")

        monkeypatch.setattr(dbmod, "rekey", rekey)
    else:
        real = security.os.replace

        def replace_(src, dst):
            if str(src) == str(new_path) and str(dst) == str(settings.keyfile_path):
                raise OSError("power cut")
            return real(src, dst)

        monkeypatch.setattr(security.os, "replace", replace_)
    assert activate(client, p["pending_id"]).status_code == 500
    monkeypatch.undo()
    client.post("/api/auth/lock")  # what a restart leaves: locked
    if stage == "before":
        saved.replace(new_path)  # the crash never got to clean it up
    assert new_path.exists()
    return p


def test_crash_before_the_rekey_leaves_the_old_sheet(client, settings, vault, monkeypatch):
    old = setup_sheet(client)
    add_account(client, "Kept")
    p = _crash_activate(monkeypatch, settings, vault, client, stage="before")
    assert check(client, old).status_code == 200  # never activated: the old sheet works
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert not settings.keyfile_path.with_name("keyfile.json.new").exists()
    assert accounts(client) == ["Kept"]
    assert rstatus(client)["status"] == "unconfirmed"  # the old block is still on record
    client.post("/api/auth/lock")
    assert check(client, "".join(p["groups"])).json()["code"] == "no_match"
    assert recover(client, old).status_code == 200


@pytest.mark.parametrize("opener", ["password", "new_sheet"])
def test_crash_after_the_rekey_is_finished_by_the_next_unlock(client, settings, vault, monkeypatch, opener):
    old = setup_sheet(client)
    add_account(client, "Kept")
    p = _crash_activate(monkeypatch, settings, vault, client, stage="after")
    new = "".join(p["groups"])
    # keyfile.json still holds the old sheet (its key opens nothing now); .new has retired it.
    r = check(client, old)
    assert r.status_code == 409 and r.json()["code"] == "outdated"
    if opener == "password":
        assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    else:
        assert recover(client, new).status_code == 200
    assert not settings.keyfile_path.with_name("keyfile.json.new").exists()
    assert accounts(client) == ["Kept"]
    assert rstatus(client) == {"status": "active", "sheet": p["sheet"], "created_at": p["created_at"]}
    client.post("/api/auth/lock")
    assert check(client, old).json()["code"] == "outdated"
    assert check(client, new).status_code == 200


# ------------------------------------------------------------------ vaults from before the recovery sheet


def test_old_keyfile_without_a_block(client, settings):
    setup_sheet(client)
    add_account(client, "Old")
    client.post("/api/auth/lock")
    params, _ = read_keyfile_full(settings.keyfile_path)
    write_json_atomic(settings.keyfile_path, params.to_json())

    assert auth_status(client)["recovery"] == {"available": False, "sheet": None}
    r = check(client, VECTOR_CODE)
    assert r.status_code == 409 and r.json() == {"detail": "This Iron Owl doesn't have a recovery sheet.", "code": "no_recovery"}
    assert recover(client, VECTOR_CODE).json()["code"] == "no_recovery"
    assert auth_status(client)["tries_left"] == 5

    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert rstatus(client) == {"status": "none", "sheet": None, "created_at": None}
    # The change-password path works without a block and doesn't invent one.
    client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert block_of(settings) is None
    p = make_sheet(client, NEW_PASSWORD)
    assert activate(client, p["pending_id"]).json()["status"] == "active"
    client.post("/api/auth/lock")
    assert recover(client, "".join(p["groups"])).status_code == 200
    assert accounts(client) == ["Old"]


# ------------------------------------------------------------------ the shared limiter


def test_wrong_codes_count_and_share_the_limiter(client, clock):
    code = setup_sheet(client)
    sheet = auth_status(client)["recovery"]["sheet"]
    client.post("/api/auth/lock")
    for left in (4, 3, 2, 1):
        r = check(client, wrong_code())
        assert r.status_code == 401
        assert r.json() == {
            "detail": "Those numbers don't match this Iron Owl.", "code": "no_match",
            "tries_left": left, "active_sheet": sheet,
        }
        # A right check does not reset the count.
        if left == 3:
            assert check(client, code).status_code == 200
            assert auth_status(client)["tries_left"] == 3
    r = check(client, wrong_code())
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert r.json() == {"detail": "too many attempts", "code": "rate_limited", "retry_after": 30}
    st = auth_status(client)
    assert st["tries_left"] == 0 and 0 < st["retry_after"] <= 30
    # One limiter: the password waits too, and so does the right sheet.
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 429
    assert check(client, code).status_code == 429
    assert recover(client, code).status_code == 429
    clock.advance(31)
    assert recover(client, code).status_code == 200
    assert auth_status(client)["tries_left"] == 5  # success resets


def test_outdated_and_unusable_have_their_own_soft_limit(client, settings, clock):
    """Not wrong tries (the shared count stays), but each costs Argon2: 10 a minute."""
    old = setup_sheet(client)
    p = make_sheet(client)
    assert activate(client, p["pending_id"]).status_code == 200
    client.post("/api/auth/lock")
    runs = {"n": 0}
    real = rec.RecoveryKdf.seed

    def counting(self, data):
        runs["n"] += 1
        return real(self, data)

    try:
        rec.RecoveryKdf.seed = counting
        for _ in range(10):
            assert check(client, old).json()["code"] == "outdated"
        r = check(client, old)
        assert r.status_code == 429 and r.json()["code"] == "rate_limited" and r.headers["retry-after"] == "60"
        assert recover(client, old).status_code == 429
        # The right sheet waits too (the check comes before Argon2), but nothing is counted.
        assert check(client, "".join(p["groups"])).status_code == 429
        assert runs["n"] == 10
    finally:
        rec.RecoveryKdf.seed = real
    st = auth_status(client)
    assert st["tries_left"] == 5 and st["retry_after"] is None
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200  # password unaffected
    client.post("/api/auth/lock")
    clock.advance(30)
    assert check(client, old).status_code == 429
    clock.advance(31)
    assert check(client, old).json()["code"] == "outdated"
    assert check(client, "".join(p["groups"])).status_code == 200


def test_password_and_sheet_failures_add_up(client):
    setup_sheet(client)
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": "wrong password!!"}).json()["tries_left"] == 4
    assert check(client, wrong_code()).json()["tries_left"] == 3
    assert client.post("/api/auth/unlock", json={"password": "wrong password!!"}).json()["tries_left"] == 2


# ------------------------------------------------------------------ tampered blocks


def _b64(n: int, fill: int = 7) -> str:
    return base64.b64encode(bytes([fill]) * n).decode()


def _retired(n: int) -> list[dict]:
    return [{"public_key": _b64(32, i), "created_at": "2026-01-01T00:00:00Z", "retired_at": "2026-01-02T00:00:00Z"}
            for i in range(n)]


def _flip(value: str) -> str:
    raw = bytearray(base64.b64decode(value))
    raw[0] ^= 1
    return base64.b64encode(bytes(raw)).decode()


TAMPER = {
    "salt_not_base64": (lambda b: b.update(salt="!!!!not base64!!!!"), "none"),
    "salt_short": (lambda b: b.update(salt=_b64(8)), "none"),
    "public_key_short": (lambda b: b.update(public_key=_b64(31)), "none"),
    "huge_memory_cost": (lambda b: b.update(memory_cost=1 << 30), "none"),
    "zero_time_cost": (lambda b: b.update(time_cost=0), "none"),
    "bad_alg": (lambda b: b["sealed"].update(alg="rot13"), "none"),
    "missing_sealed": (lambda b: b.pop("sealed"), "none"),
    "ct_short": (lambda b: b["sealed"].update(ct=_b64(47)), "none"),
    "extra_key": (lambda b: b.update(sheet="482-913"), "none"),
    "confirmed_not_bool": (lambda b: b.update(confirmed=1), "none"),
    "version_2": (lambda b: b.update(version=2), "none"),
    "eleven_retired": (lambda b: b.update(retired=_retired(11)), "none"),
    "retired_bad_date": (lambda b: b.update(retired=[{**_retired(1)[0], "retired_at": "yesterday"}]), "none"),
    "bad_mac": (lambda b: b.update(mac=_flip(b["mac"])), "stale"),
    "ct_flipped": (lambda b: b["sealed"].update(ct=_flip(b["sealed"]["ct"])), "stale"),
    "for_salt_changed": (lambda b: b["sealed"].update(for_salt=_b64(16, 1)), "stale"),
}


@pytest.mark.parametrize("name", list(TAMPER))
def test_tampered_block_fails_closed(client, settings, name):
    code = setup_sheet(client)
    mutate, expected = TAMPER[name]
    data = json.loads(settings.keyfile_path.read_text())
    mutate(data["recovery"])
    settings.keyfile_path.write_text(json.dumps(data))

    assert rstatus(client)["status"] == expected
    st = auth_status(client)
    assert st["recovery"]["available"] is (expected == "stale")
    client.post("/api/auth/lock")
    r = check(client, code)
    assert r.status_code != 500
    if expected == "none":
        assert r.status_code == 409 and r.json()["code"] == "no_recovery"
    elif name == "bad_mac":
        assert r.status_code == 200  # the key is still sealed to this sheet; only the MAC is off
    else:
        assert r.status_code == 409 and r.json()["code"] == "unusable"
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    # A block that fails its MAC is never re-sealed by a password change.
    r = client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    after = block_of(settings)
    params = read_keyfile_full(settings.keyfile_path)[0]
    assert after is None or after.sealed.for_salt != params.salt
    assert rstatus(client)["status"] == expected


def test_not_an_object_block(client, settings):
    setup_sheet(client)
    data = json.loads(settings.keyfile_path.read_text())
    data["recovery"] = "surprise"
    settings.keyfile_path.write_text(json.dumps(data))
    assert rstatus(client)["status"] == "none"
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200


def test_a_planted_public_key_never_gets_the_key(client, settings):
    """Someone who can write data/ swaps in their own sheet's public key (they can't fix the MAC)."""
    code = setup_sheet(client)
    block = block_of(settings)
    attacker = wrong_code()
    planted = rec.public_key(block.kdf.seed(rec.data_digits(attacker)))
    data = json.loads(settings.keyfile_path.read_text())
    data["recovery"]["public_key"] = base64.b64encode(planted).decode()
    settings.keyfile_path.write_text(json.dumps(data))
    assert rstatus(client)["status"] == "stale"

    r = client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    params, after = read_keyfile_full(settings.keyfile_path)
    assert after.public_key == planted and after.sealed.for_salt != params.salt  # not re-sealed
    # No new sheet from Settings reuses the planted block either.
    p = make_sheet(client, NEW_PASSWORD)
    assert activate(client, p["pending_id"]).status_code == 200
    assert block_of(settings).retired == ()  # the planted key isn't carried over
    client.post("/api/auth/lock")
    assert check(client, attacker).json()["code"] == "no_match"
    assert check(client, code).json()["code"] == "no_match"


# ------------------------------------------------------------------ no leaks


def test_the_code_is_never_logged_or_echoed(client, app, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    codes = ["".join(r.json()["recovery"]["groups"])]
    bodies: list[str] = []

    def keep(resp):
        if resp.status_code != 200 or resp.request.url.path.endswith("/check"):
            bodies.append(resp.text)
        return resp

    p = keep(client.post("/api/recovery", json={"current_password": PASSWORD})).json()
    codes.append("".join(p["groups"]))
    keep(activate(client, p["pending_id"]))
    client.post("/api/auth/lock")
    old, new = codes
    typo = old[:18] + ("0" if old[18] != "0" else "1") + old[19:]
    for text in (old, new, typo, old + "7", old[:30], wrong_code()):
        keep(check(client, text))
    keep(recover(client, new, "short"))
    keep(client.post("/api/auth/recover", json={"code": int(old[:18]), "new_password": NEW_PASSWORD}))
    keep(client.post("/api/auth/recover/check", json={"code": old * 4}))
    keep(recover(client, new))

    secrets_ = set()
    for code in codes:
        groups = rec.split_groups(code)
        secrets_.update({code, rec.data_digits(code).decode(), " ".join(groups), "-".join(groups)})
        secrets_.update(groups)
        secrets_.update(f"{g[:3]} {g[3:]}" for g in groups)
    assert len(bodies) >= 8
    for text in [caplog.text, *bodies]:
        for s in secrets_:
            assert s not in text


# ------------------------------------------------------------------ backups and restore


def test_backup_carries_the_block_and_names_the_sheet(client, settings):
    setup_sheet(client)
    sheet = auth_status(client)["recovery"]["sheet"]
    entries = entries_of(download(client).content)
    assert json.loads(entries["manifest.json"])["recovery_sheet"] == sheet
    assert entries["keyfile.json"] == settings.keyfile_path.read_bytes()
    assert "recovery" in json.loads(entries["keyfile.json"])


def test_backup_without_a_block_names_no_sheet(client, settings):
    setup_sheet(client)
    params, _ = read_keyfile_full(settings.keyfile_path)
    write_json_atomic(settings.keyfile_path, params.to_json())
    assert json.loads(entries_of(download(client).content)["manifest.json"])["recovery_sheet"] is None


def test_restore_at_setup_keeps_the_backup_sheet(client, fresh_client):
    code = setup_sheet(client)
    add_account(client, "From backup")
    backup = download(client).content
    assert restore(fresh_client, backup).status_code == 200
    fresh_client.post("/api/auth/lock")
    assert recover(fresh_client, code).status_code == 200
    assert accounts(fresh_client) == ["From backup"]


def test_restore_at_setup_drops_a_block_that_fails_its_mac(client, fresh_client):
    setup_sheet(client)
    entries = entries_of(download(client).content)
    keyfile = json.loads(entries["keyfile.json"])
    keyfile["recovery"]["mac"] = _flip(keyfile["recovery"]["mac"])
    entries["keyfile.json"] = json.dumps(keyfile).encode()
    assert restore(fresh_client, zip_bytes(entries)).status_code == 200
    assert auth_status(fresh_client)["recovery"] == {"available": False, "sheet": None}
    assert rstatus(fresh_client)["status"] == "none"


def test_in_app_restore_keeps_the_current_sheet(client, settings):
    first = setup_sheet(client)
    add_account(client, "Backed up")
    backup = download(client).content
    p = make_sheet(client)
    assert activate(client, p["pending_id"]).status_code == 200
    add_account(client, "After the backup")
    before = settings.keyfile_path.read_bytes()

    r = restore(client, backup, current_password=PASSWORD)
    assert r.status_code == 200, r.text
    assert accounts(client) == ["Backed up"]
    assert rstatus(client) == {"status": "active", "sheet": p["sheet"], "created_at": p["created_at"]}
    (moved,) = [d for d in settings.data_dir.iterdir() if d.name.startswith("pre-restore-")]
    assert (moved / "keyfile.json").read_bytes() == before  # the set-aside vault keeps its block

    client.post("/api/auth/lock")
    r = check(client, first)
    assert r.status_code == 409 and r.json()["code"] == "outdated" and r.json()["active_sheet"] == p["sheet"]
    assert recover(client, "".join(p["groups"])).status_code == 200
    assert accounts(client) == ["Backed up"]


def test_in_app_restore_of_another_install(client, fresh_client):
    mine = setup_sheet(client)
    other_pw = "the other vault password"
    theirs = setup_sheet(fresh_client, other_pw)
    add_account(fresh_client, "Other bank")
    backup = download(fresh_client, other_pw).content
    assert restore(client, backup, password=other_pw, current_password=PASSWORD).status_code == 200
    client.post("/api/auth/lock")
    # A different install has its own KDF salt, so its sheet can't be told apart from a stranger's.
    assert check(client, theirs).json()["code"] == "no_match"
    assert recover(client, mine).status_code == 200
    assert accounts(client) == ["Other bank"]


# ------------------------------------------------------------------ final review fixes


def test_failed_pending_rederive_drops_the_new_sheet(client, settings, vault, monkeypatch):
    """Change password with a new sheet pending: if deriving the pending key again from the
    new password fails, the pending sheet is dropped. Done then refuses, and the vault is
    never rekeyed to a wiped (all-zero) key."""
    old = setup_sheet(client)
    add_account(client, "Kept")
    p = make_sheet(client)
    ref = vault._pending.key
    real = KeyParams.derive
    calls = {"n": 0}

    def derive(self, password):
        calls["n"] += 1
        if calls["n"] == 3:  # current password, new vault key, then the pending re-derive
            raise MemoryError("argon2")
        return real(self, password)

    monkeypatch.setattr(KeyParams, "derive", derive)
    r = client.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    monkeypatch.undo()
    assert calls["n"] == 3
    assert r.status_code == 200  # the password change itself went through
    assert vault._pending is None and ref == bytearray(32)
    r = activate(client, p["pending_id"])
    assert r.status_code == 409 and r.json()["code"] == "pending_expired"
    assert not dbmod.verify_key(settings.db_path, bytes(32))
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200
    assert accounts(client) == ["Kept"]
    client.post("/api/auth/lock")
    assert check(client, "".join(p["groups"])).json()["code"] == "no_match"
    assert check(client, old).status_code == 200  # the old sheet was re-sealed and still works


@pytest.mark.parametrize("bad", [bytearray(32), bytearray(b"\x01" * 31), bytearray(b"\x01" * 33)])
def test_done_refuses_an_unusable_pending_key(client, settings, vault, bad):
    old = setup_sheet(client)
    p = make_sheet(client)
    vault._pending.key = bad
    r = activate(client, p["pending_id"])
    assert r.status_code == 409 and r.json()["code"] == "pending_expired"
    assert vault._pending is None and vault.unlocked
    assert not dbmod.verify_key(settings.db_path, bytes(32))
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    client.post("/api/auth/lock")
    assert check(client, old).status_code == 200


def _new_path(settings):
    return settings.keyfile_path.with_name("keyfile.json.new")


def test_done_rename_keeps_failing_says_finish_on_unlock(client, settings, vault, monkeypatch):
    """keyfile.json.new can't replace keyfile.json (e.g. an antivirus holds it): retried, then
    a distinct error. The vault is locked and the next unlock finishes the new sheet."""
    old = setup_sheet(client)
    add_account(client, "Kept")
    p = make_sheet(client)
    real = security.os.replace
    tries = {"n": 0}

    def replace_(src, dst):
        if str(src) == str(_new_path(settings)):
            tries["n"] += 1
            raise PermissionError("sharing violation")
        return real(src, dst)

    monkeypatch.setattr(security, "REPLACE_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(security.os, "replace", replace_)
    r = activate(client, p["pending_id"])
    monkeypatch.undo()
    assert tries["n"] == 4  # the first try and three retries
    assert r.status_code == 500
    assert r.json() == {"detail": "Iron Owl couldn't finish this step. Unlock Iron Owl to complete it.",
                        "code": "finish_on_unlock"}
    assert not vault.unlocked and _new_path(settings).exists()
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert not _new_path(settings).exists()
    assert rstatus(client) == {"status": "active", "sheet": p["sheet"], "created_at": p["created_at"]}
    assert accounts(client) == ["Kept"]
    client.post("/api/auth/lock")
    assert check(client, old).json()["code"] == "outdated"
    assert check(client, "".join(p["groups"])).status_code == 200


def test_done_reopen_fails_says_finish_on_unlock(client, settings, vault, monkeypatch):
    setup_sheet(client)
    p = make_sheet(client)
    real_open = dbmod.Database.open
    real_replace = security.os.replace
    armed = {"x": False}

    def replace_(src, dst):
        out = real_replace(src, dst)
        if str(src) == str(_new_path(settings)):
            armed["x"] = True
        return out

    def open_(self, key, **kw):
        if armed["x"]:
            armed["x"] = False
            raise OSError("power cut")
        return real_open(self, key, **kw)

    monkeypatch.setattr(security.os, "replace", replace_)
    monkeypatch.setattr(dbmod.Database, "open", open_)
    r = activate(client, p["pending_id"])
    monkeypatch.undo()
    assert r.status_code == 500 and r.json()["code"] == "finish_on_unlock"
    assert not vault.unlocked and vault._pending is None
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert rstatus(client)["sheet"] == p["sheet"]


def test_done_rename_retries_a_brief_sharing_violation(client, settings, monkeypatch):
    setup_sheet(client)
    p = make_sheet(client)
    real = security.os.replace
    tries = {"n": 0}

    def replace_(src, dst):
        if str(src) == str(_new_path(settings)):
            tries["n"] += 1
            if tries["n"] <= 2:
                raise PermissionError("sharing violation")
        return real(src, dst)

    monkeypatch.setattr(security, "REPLACE_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(security.os, "replace", replace_)
    r = activate(client, p["pending_id"])
    monkeypatch.undo()
    assert r.status_code == 200 and r.json()["sheet"] == p["sheet"] and tries["n"] == 3


def _race_verify(monkeypatch, fire):
    """Run ``fire(n)`` right after the backup copy is verified (n = 1, 2, ...): the moment
    between the database copy and the keyfile read."""
    from app.services import backup as bsvc
    real = dbmod.verify_key
    n = {"n": 0}

    def verify(path, key):
        ok = real(path, key)
        if path.parent.name.startswith(".backup-"):
            n["n"] += 1
            fire(n["n"])
        return ok

    monkeypatch.setattr(bsvc.dbmod, "verify_key", verify)
    return n


def test_backup_racing_a_new_sheet_is_made_again(client, settings, vault, monkeypatch):
    """Done lands between the backup's database copy and its keyfile read: that copy is thrown
    away and made again, so the backup opens with its own keyfile."""
    from app.services import backup as bsvc
    setup_sheet(client)
    p = make_sheet(client)
    n = _race_verify(monkeypatch, lambda i: i == 1 and vault.activate_pending(p["pending_id"]))
    data = bsvc.build_backup(vault)
    monkeypatch.undo()
    assert n["n"] == 2
    e = entries_of(data)
    db_copy = settings.data_dir / "race.db"
    db_copy.write_bytes(e["fintrack.db"])
    assert e["keyfile.json"] == settings.keyfile_path.read_bytes()
    assert dbmod.verify_key(db_copy, KeyParams.from_json(json.loads(e["keyfile.json"])).derive(PASSWORD))
    assert json.loads(e["manifest.json"])["recovery_sheet"] == p["sheet"]
    assert dbmod.verify_key(db_copy, offline_key(e["keyfile.json"], "".join(p["groups"])))


def test_backup_that_keeps_racing_fails_clearly(client, settings, vault, monkeypatch):
    """Changed again on the second try: the backup fails (never a file that can't be opened)."""
    setup_sheet(client)
    passwords = [PASSWORD, NEW_PASSWORD, THIRD_PASSWORD]

    def fire(i):
        if i <= 2:
            vault.change_password(passwords[i - 1], passwords[i])

    n = _race_verify(monkeypatch, fire)
    r = download(client)
    monkeypatch.undo()
    assert n["n"] == 2
    assert r.status_code == 409 and r.json() == {
        "detail": "Your password or recovery sheet changed while the backup was being made. Please try again.",
        "code": "backup_retry",
    }
    assert not [d for d in settings.data_dir.iterdir() if d.name.startswith(".backup-")]


def test_restore_at_setup_marks_the_backup_sheet_unconfirmed(client, fresh_client):
    """The backup's own sheet is kept, but it may be the lost one: Settings and Home ask for
    a new sheet."""
    code = setup_sheet(client)
    sheet = rstatus(client)["sheet"]
    assert client.post("/api/recovery/confirm", json={"sheet": sheet}).json()["status"] == "active"
    backup = download(client).content
    assert restore(fresh_client, backup).status_code == 200
    assert rstatus(fresh_client)["status"] == "unconfirmed" and rstatus(fresh_client)["sheet"] == sheet
    fresh_client.post("/api/auth/lock")
    assert recover(fresh_client, code).status_code == 200  # it still works


def test_in_app_restore_over_a_stale_sheet_marks_the_backup_sheet_unconfirmed(client, fresh_client, vault):
    setup_sheet(client)
    security.write_pub_rows(vault.db, None, None)  # e.g. a vault from before the rows
    assert rstatus(client)["status"] == "stale"
    other_pw = "the other vault password"
    setup_sheet(fresh_client, other_pw)
    theirs = rstatus(fresh_client)["sheet"]
    assert fresh_client.post("/api/recovery/confirm", json={"sheet": theirs}).json()["status"] == "active"
    backup = download(fresh_client, other_pw).content
    assert restore(client, backup, password=other_pw, current_password=PASSWORD).status_code == 200
    st = rstatus(client)
    assert st["status"] == "unconfirmed" and st["sheet"] == theirs


def test_an_expired_pending_sheet_is_dropped_on_the_next_request(client, vault, clock):
    setup_sheet(client)
    make_sheet(client)
    ref = vault._pending.key
    for _ in range(2):
        clock.advance(14 * 60)
        rstatus(client)  # activity keeps the session alive
    assert vault._pending is not None
    clock.advance(2 * 60 + 1)
    rstatus(client)
    assert vault._pending is None and ref == bytearray(32)
