"""App-window states (D4): window presence, "already open", "Open here" handoff, close = lock,
heartbeats, lock reasons and the persisted wrong-try count."""
from __future__ import annotations

import json
import time

import pytest

from app import presence as presence_mod
from app.main import create_app
from app.presence import MAX_TABS, PRESENCE_TTL, TAB_HEADER, Presence, valid_tab
from app.routers import app as app_routes
from app.security import SESSION_HEADER, RateLimiter

from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient, make_settings

TAB_A = "tab-aaaa-0001"
TAB_B = "tab-bbbb-0002"
TAB_C = "tab-cccc-0003"
WRONG = "wrong password!!"


def hdr(tab: str | None = None, **extra: str) -> dict:
    headers = dict(extra)
    if tab is not None:
        headers[TAB_HEADER] = tab
    return headers


def status(c, tab: str | None = None) -> dict:
    r = c.get("/api/auth/status", headers=hdr(tab))
    assert r.status_code == 200, r.text
    return r.json()


def alive(c, tab: str | None = TAB_A, **extra: str):
    return c.post("/api/app/alive", headers=hdr(tab, **extra))


def other_client(app, cookie_from=None) -> SessionClient:
    """Another window. ``cookie_from``: the same browser (windows share the cookie jar)."""
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    if cookie_from is not None:
        c.cookies.set("ft_session", cookie_from.cookies.get("ft_session"))
    return c


@pytest.fixture
def state(app):
    return app.state.fintrack


@pytest.fixture
def window_a(clock, client):
    """Window A set FinTrack up (clock patched first, so presence and idle use it)."""
    r = client.post("/api/auth/setup", json={"password": PASSWORD}, headers=hdr(TAB_A))
    assert r.status_code == 200, r.text
    return client


# ------------------------------------------------------------------ presence rules


def test_tab_ids_are_validated():
    assert valid_tab("0b6c3f1e-8a7d-4a1b-9c7e-2f5d6a4b3c21") == "0b6c3f1e-8a7d-4a1b-9c7e-2f5d6a4b3c21"
    assert valid_tab("abcdefgh") == "abcdefgh"
    for bad in (None, "", "short", "x" * 65, "has space1", "tab_under", "tab/slash1", "é" * 10):
        assert valid_tab(bad) is None, bad


def test_presence_ttl_close_and_cap(clock):
    p = Presence()
    assert not p.present(TAB_A) and not p.window_open() and not p.present(None)
    p.seen(TAB_A)
    assert p.present(TAB_A) and p.window_open()
    clock.advance(PRESENCE_TTL)
    assert p.present(TAB_A)
    clock.advance(1)
    assert not p.present(TAB_A) and not p.window_open()
    p.seen(TAB_A)
    p.request_close(TAB_A)
    assert p.closing(TAB_A) and not p.present(TAB_A) and not p.window_open()
    clock.advance(presence_mod.CLOSE_GRACE - 1)
    assert not p.close_due(TAB_A) and p.due_closes() == []
    p.seen(TAB_A)  # came back (a reload): the close is cancelled
    assert not p.closing(TAB_A) and p.present(TAB_A)
    clock.advance(presence_mod.CLOSE_GRACE + 1)
    assert not p.close_due(TAB_A)
    p.request_close(TAB_A)
    clock.advance(presence_mod.CLOSE_GRACE)
    assert p.due_closes() == [TAB_A]
    assert p.close_due(TAB_A) and not p.close_due(TAB_A)  # once; then forgotten
    assert p.last_seen(TAB_A) is None

    for i in range(MAX_TABS + 8):
        p.seen(f"tab-{i:08d}")
    assert len(p) == MAX_TABS
    assert p.last_seen("tab-00000000") is None and p.last_seen(f"tab-{MAX_TABS + 7:08d}") is not None
    p.seen("tab-00000008")  # refreshed: now the newest, so the next new one evicts 9
    p.seen("tab-new-00001")
    assert p.last_seen("tab-00000008") is not None and p.last_seen("tab-00000009") is None


def test_the_tab_map_is_capped_over_http(clock, client, state):
    for i in range(MAX_TABS + 10):
        status(client, f"tab-{i:08d}")
    assert len(state.presence) == MAX_TABS


# ------------------------------------------------------------------------- status


def test_status_fields_before_setup(client):
    body = status(client, TAB_A)
    assert body == {
        "initialized": False, "unlocked": False, "auto_lock_minutes": 15,
        "recovery": {"available": False, "sheet": None},
        "open_elsewhere": False, "lock_reason": None,
        "retry_after": None, "tries_left": 5, "support_contact": None,
    }


def test_status_with_a_bad_or_missing_tab_still_answers(window_a, app):
    b = other_client(app)
    for headers in ({}, {TAB_HEADER: "bad"}, {TAB_HEADER: "x" * 100}):
        r = b.get("/api/auth/status", headers=headers)
        assert r.status_code == 200
        # no tab id: still "open elsewhere" (A is bound and present)
        assert r.json()["open_elsewhere"] is True and r.json()["unlocked"] is False


def test_status_countdown_and_lock_reasons(window_a, clock):
    body = status(window_a, TAB_A)
    assert body["unlocked"] is True and body["open_elsewhere"] is False and body["lock_reason"] is None
    window_a.post("/api/auth/lock")
    assert status(window_a, TAB_A)["lock_reason"] == "manual"
    for left in (4, 3, 2, 1):
        r = window_a.post("/api/auth/unlock", json={"password": WRONG}, headers=hdr(TAB_A))
        assert r.json() == {"detail": "invalid password", "code": "wrong_password", "tries_left": left}
        assert status(window_a, TAB_A)["tries_left"] == left
    assert window_a.post("/api/auth/unlock", json={"password": WRONG}).status_code == 429
    body = status(window_a, TAB_A)
    assert body["tries_left"] == 0 and body["retry_after"] == 30
    clock.advance(10)
    assert status(window_a, TAB_A)["retry_after"] == 20  # a reload resumes the countdown
    clock.advance(21)
    assert window_a.post("/api/auth/unlock", json={"password": PASSWORD}, headers=hdr(TAB_A)).status_code == 200
    assert status(window_a, TAB_A)["tries_left"] == 5


def test_lock_body_reason(window_a):
    r = window_a.post("/api/auth/lock", json={"reason": "idle"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert status(window_a)["lock_reason"] == "idle"
    for bad in ({"reason": "closed"}, {"reason": "shutdown"}, {"reason": "manual", "x": 1}):
        assert window_a.post("/api/auth/lock", json=bad).status_code == 422
    window_a.post("/api/auth/unlock", json={"password": PASSWORD}, headers=hdr(TAB_A))
    assert window_a.post("/api/auth/lock", json={}).status_code == 200
    assert status(window_a)["lock_reason"] == "manual"
    # locking an already locked vault doesn't change why it locked
    window_a.post("/api/auth/lock", json={"reason": "idle"})
    assert status(window_a)["lock_reason"] == "manual"


def test_idle_lock_reason(window_a, clock):
    clock.advance(15 * 60 + 1)
    body = status(window_a, TAB_A)
    assert body["unlocked"] is False and body["lock_reason"] == "idle"


def test_shutdown_lock_reason(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    assert app.state.fintrack.vault.lock_reason == "shutdown"


# --------------------------------------------------------------- open elsewhere


def test_open_elsewhere_rules(window_a, app, clock):
    b = other_client(app)
    body = status(b, TAB_B)
    assert body["unlocked"] is False and body["open_elsewhere"] is True and body["lock_reason"] is None
    # the bound window itself is never "elsewhere"
    assert status(window_a, TAB_A)["open_elsewhere"] is False
    # a caller with the valid session is never "elsewhere", whatever its tab id says
    assert status(window_a, TAB_B)["open_elsewhere"] is False
    # A not heard from within the TTL: gone (crashed), so B gets the password screen
    clock.advance(PRESENCE_TTL + 1)
    assert status(b, TAB_B)["open_elsewhere"] is False
    assert alive(window_a, TAB_A).status_code == 200
    assert status(b, TAB_B)["open_elsewhere"] is True
    # locked: nothing is open elsewhere
    window_a.post("/api/auth/lock")
    assert status(b, TAB_B)["open_elsewhere"] is False


def test_unlock_without_a_tab_is_never_elsewhere(clock, unlocked, app):
    assert status(other_client(app), TAB_B)["open_elsewhere"] is False


def test_a_closing_window_is_not_elsewhere(window_a, app):
    assert window_a.post("/api/app/closing", headers=hdr(TAB_A)).status_code == 204
    assert status(other_client(app), TAB_B)["open_elsewhere"] is False


# ------------------------------------------------------------ supersede -> moved


def test_unlock_in_another_window_moves_the_session(window_a, app, vault):
    b = other_client(app)
    status(b, TAB_B)
    assert b.post("/api/auth/unlock", json={"password": PASSWORD}, headers=hdr(TAB_B)).status_code == 200
    assert b.get("/api/accounts").status_code == 200
    r = window_a.get("/api/accounts")
    assert r.status_code == 401 and r.json() == {"detail": "moved"}
    # the same browser: A now carries B's cookie with its old token -> still "moved"
    window_a.cookies.set("ft_session", b.cookies.get("ft_session"))
    assert window_a.get("/api/accounts").json() == {"detail": "moved"}
    body = status(window_a, TAB_A)
    assert body["unlocked"] is False and body["open_elsewhere"] is True and body["lock_reason"] == "moved"

    # a moved window can't lock the other one (nor clear the shared cookie)
    r = window_a.post("/api/auth/lock", json={"reason": "idle"})
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert vault.unlocked and b.get("/api/accounts").status_code == 200

    # B locks: the moved marker is gone, A is simply locked (with B's reason)
    b.post("/api/auth/lock")
    assert window_a.get("/api/accounts").json() == {"detail": "locked"}
    body = status(window_a, TAB_A)
    assert body["open_elsewhere"] is False and body["lock_reason"] == "manual"


def test_same_window_unlocking_again_is_not_a_move(window_a):
    old_token = window_a.headers[SESSION_HEADER]
    assert window_a.post("/api/auth/unlock", json={"password": PASSWORD}, headers=hdr(TAB_A)).status_code == 200
    r = window_a.get("/api/accounts", headers={SESSION_HEADER: old_token})
    assert r.json() == {"detail": "locked"}


def test_change_password_keeps_the_window(window_a, vault):
    old_token = window_a.headers[SESSION_HEADER]
    r = window_a.post("/api/auth/change-password",
                      json={"current_password": PASSWORD, "new_password": PASSWORD + "!"})
    assert r.status_code == 200
    assert vault.bound_tab() == TAB_A  # no tab header on this call: the binding stays
    assert window_a.get("/api/accounts", headers={SESSION_HEADER: old_token}).json() == {"detail": "locked"}
    assert window_a.get("/api/accounts").status_code == 200


def test_recover_in_another_window_moves_the_session(clock, client, app, vault):
    r = client.post("/api/auth/setup", json={"password": PASSWORD}, headers=hdr(TAB_A))
    groups = r.json()["recovery"]["groups"]
    b = other_client(app)
    r = b.post("/api/auth/recover", json={"code": " ".join(groups), "new_password": PASSWORD + "?"},
               headers=hdr(TAB_B))
    assert r.status_code == 200, r.text
    assert vault.bound_tab() == TAB_B
    assert client.get("/api/accounts").json() == {"detail": "moved"}


# ------------------------------------------------------------------------ handoff


def test_handoff_moves_the_session(window_a, app, vault, state, clock):
    b = other_client(app, cookie_from=window_a)
    assert status(b, TAB_B)["open_elsewhere"] is True
    r = window_a.post("/api/app/handoff", json={"to_tab": TAB_B})
    assert r.status_code == 200 and set(r.json()) == {"session_token"}
    token = r.json()["session_token"]
    assert "set-cookie" not in r.headers  # the cookie (shared) stays
    b.headers[SESSION_HEADER] = token
    assert b.get("/api/accounts").status_code == 200
    assert status(b, TAB_B)["unlocked"] is True
    assert window_a.get("/api/accounts").json() == {"detail": "moved"}
    body = status(window_a, TAB_A)
    assert body["lock_reason"] == "moved" and body["open_elsewhere"] is True
    assert vault.bound_tab() == TAB_B
    # the old window can't hand it back
    r = window_a.post("/api/app/handoff", json={"to_tab": TAB_A})
    assert r.status_code == 401 and r.json() == {"detail": "moved"}
    # closing A no longer locks anything; closing B does
    window_a.post("/api/app/closing", headers=hdr(TAB_A))
    assert state.presence.last_seen(TAB_A) is None and vault.unlocked
    b.post("/api/app/closing", headers=hdr(TAB_B))
    clock.advance(presence_mod.CLOSE_GRACE)
    app_routes.finish_due_closes(state)
    assert not vault.unlocked and vault.lock_reason == "closed"


def test_handoff_errors(window_a, app, clock, state):
    b = other_client(app)
    # 401: no session / wrong token
    r = b.post("/api/app/handoff", json={"to_tab": TAB_C})
    assert r.status_code == 401 and r.json() == {"detail": "locked"}
    # 400: malformed, or the session's own window
    for bad in ("short", "has space in it", "x" * 65):
        r = window_a.post("/api/app/handoff", json={"to_tab": bad})
        assert r.status_code == 400 and r.json() == {"detail": "invalid tab"}
    status(window_a, TAB_A)
    assert window_a.post("/api/app/handoff", json={"to_tab": TAB_A}).status_code == 400
    # 422: body shape
    assert window_a.post("/api/app/handoff", json={}).status_code == 422
    assert window_a.post("/api/app/handoff", json={"to_tab": TAB_B, "x": 1}).status_code == 422
    # 409: never seen, not seen within the TTL, or closing
    r = window_a.post("/api/app/handoff", json={"to_tab": TAB_C})
    assert r.status_code == 409 and r.json() == {"detail": "window not found"}
    status(b, TAB_C)
    clock.advance(PRESENCE_TTL + 1)
    assert window_a.post("/api/app/handoff", json={"to_tab": TAB_C}).status_code == 409
    status(b, TAB_C)
    b.post("/api/app/closing", headers=hdr(TAB_C))
    assert window_a.post("/api/app/handoff", json={"to_tab": TAB_C}).status_code == 409
    # 403: CSRF like every POST
    r = window_a.post("/api/app/handoff", json={"to_tab": TAB_C}, headers={"X-FinTrack": "0"})
    assert r.status_code == 403
    # nothing above changed the session
    assert window_a.get("/api/accounts").status_code == 200


def test_handoff_drops_a_pending_recovery_sheet(window_a, app, vault):
    r = window_a.post("/api/recovery", json={"current_password": PASSWORD})
    assert r.status_code == 200, r.text
    assert vault._pending is not None
    status(other_client(app), TAB_B)
    assert window_a.post("/api/app/handoff", json={"to_tab": TAB_B}).status_code == 200
    assert vault._pending is None


# ------------------------------------------------------------------------ closing


@pytest.fixture
def before_lock_calls(vault):
    calls: list[int] = []
    original = vault.before_lock

    def hook():
        calls.append(1)
        return original() if original else None

    vault.before_lock = hook
    return calls


def test_closing_the_bound_window_locks_after_the_grace(window_a, clock, state, vault, before_lock_calls, app):
    r = window_a.post("/api/app/closing", headers=hdr(TAB_A))
    assert r.status_code == 204 and r.content == b""
    assert vault.unlocked and state.presence.closing(TAB_A)
    assert app_routes.finish_close(state, TAB_A) is False  # grace not over yet
    clock.advance(presence_mod.CLOSE_GRACE)
    app_routes.finish_due_closes(state)
    assert not vault.unlocked and before_lock_calls == [1]
    body = status(other_client(app), TAB_B)
    assert body["lock_reason"] == "closed" and body["open_elsewhere"] is False
    assert window_a.get("/api/accounts").json() == {"detail": "locked"}


def test_a_reload_within_the_grace_stays_unlocked(window_a, clock, state, vault, before_lock_calls):
    window_a.post("/api/app/closing", headers=hdr(TAB_A))
    clock.advance(2)
    status(window_a, TAB_A)  # the reloaded page asks for status: close cancelled
    clock.advance(presence_mod.CLOSE_GRACE + 1)
    app_routes.finish_due_closes(state)
    assert app_routes.finish_close(state, TAB_A) is False
    assert vault.unlocked and before_lock_calls == []
    # alive cancels it too
    window_a.post("/api/app/closing", headers=hdr(TAB_A))
    alive(window_a, TAB_A)
    clock.advance(presence_mod.CLOSE_GRACE + 1)
    app_routes.finish_due_closes(state)
    assert vault.unlocked


def test_closing_another_window_never_locks(window_a, app, clock, state, vault):
    b = other_client(app, cookie_from=window_a)
    status(b, TAB_B)
    assert b.post("/api/app/closing", headers=hdr(TAB_B)).status_code == 204
    assert state.presence.last_seen(TAB_B) is None and not state.presence.closing(TAB_B)
    # even with the right cookie, a tab id alone is not the session
    assert b.post("/api/app/closing", headers=hdr(TAB_C)).status_code == 204
    clock.advance(presence_mod.CLOSE_GRACE + 1)
    app_routes.finish_due_closes(state)
    assert vault.unlocked and window_a.get("/api/accounts").status_code == 200


def test_closing_needs_csrf_and_a_tab(window_a, vault):
    assert window_a.post("/api/app/closing").status_code == 400
    assert window_a.post("/api/app/closing", headers=hdr("bad")).status_code == 400
    assert window_a.post("/api/app/closing", headers=hdr(TAB_A, **{"X-FinTrack": ""})).status_code == 403
    r = window_a.post("/api/app/closing", headers=hdr(TAB_A, **{"Content-Type": "text/plain"}), content=b"x")
    assert r.status_code == 403


def test_closing_while_locked_is_harmless(client, state):
    assert client.post("/api/app/closing", headers=hdr(TAB_A)).status_code == 204
    assert not state.presence.closing(TAB_A)


def test_close_timer_locks_in_real_time(monkeypatch, client, vault):
    monkeypatch.setattr(presence_mod, "CLOSE_GRACE", 0.05)
    monkeypatch.setattr(app_routes, "CLOSE_GRACE", 0.05)
    monkeypatch.setattr(app_routes, "CLOSE_TIMER_SLACK", 0.05)
    client.post("/api/auth/setup", json={"password": PASSWORD}, headers=hdr(TAB_A))
    assert client.post("/api/app/closing", headers=hdr(TAB_A)).status_code == 204
    deadline = time.monotonic() + 5
    while vault.unlocked and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not vault.unlocked and vault.lock_reason == "closed"


# -------------------------------------------------------------------------- alive


def test_alive_fields_csrf_and_tab(window_a, app, state):
    r = alive(window_a, TAB_A)
    assert r.status_code == 200
    assert r.json() == {"open_elsewhere": False, "unlocked": True, "lock_reason": None, "boot_id": state.boot_id}
    body = alive(other_client(app), TAB_B).json()
    assert body["open_elsewhere"] is True and body["unlocked"] is False
    assert alive(window_a, None).status_code == 400
    assert alive(window_a, "no").json() == {"detail": "invalid tab"}
    assert alive(window_a, TAB_A, **{"X-FinTrack": "0"}).status_code == 403
    r = window_a.post("/api/app/alive", headers=hdr(TAB_A, Origin="http://evil.example"))
    assert r.status_code == 403


def test_alive_is_not_activity(window_a, clock):
    clock.advance(14 * 60)
    assert alive(window_a, TAB_A).json()["unlocked"] is True
    clock.advance(60 + 1)
    body = alive(window_a, TAB_A).json()  # the idle lock still fires
    assert body["unlocked"] is False and body["lock_reason"] == "idle"


def test_status_is_not_activity_either(window_a, clock, vault):
    clock.advance(14 * 60)
    status(window_a, TAB_A)
    clock.advance(60 + 1)
    vault.check_idle()
    assert not vault.unlocked


# ------------------------------------------------------------------ self-exit


class Exits:
    def __init__(self) -> None:
        self.codes: list[int] = []

    def __call__(self, code: int) -> None:
        self.codes.append(code)


def test_only_alive_is_a_heartbeat(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path, exit_when_unused_seconds=60), fake_plaid)
    exits = Exits()
    app.state.request_exit = exits
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)  # no lifespan: ticks by hand
    watcher = app.state.fintrack.exit_watcher
    assert watcher.enabled and watcher.max_misses == 2
    c.get("/api/auth/status", headers=hdr(TAB_A))
    c.get("/api/health")
    c.post("/api/auth/lock")
    c.get("/api/accounts")
    alive(c, None)  # refused (no tab): not a heartbeat
    alive(c, TAB_A, **{"X-FinTrack": "0"})  # refused (CSRF): not a heartbeat
    assert watcher._heard is False
    watcher.tick()
    assert watcher.misses == 1
    assert alive(c, TAB_A).status_code == 200
    assert watcher._heard is True
    watcher.tick()
    assert watcher.misses == 0
    watcher.tick()
    watcher.tick()
    assert exits.codes == [0]


def test_an_unlocked_vault_keeps_the_server_up(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path, exit_when_unused_seconds=30), fake_plaid)
    exits = Exits()
    app.state.request_exit = exits
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    c.post("/api/auth/setup", json={"password": PASSWORD}, headers=hdr(TAB_A))
    watcher = app.state.fintrack.exit_watcher
    for _ in range(5):
        watcher.tick()
    assert exits.codes == []
    c.post("/api/auth/lock")
    watcher.tick()
    assert exits.codes == [0]


# ------------------------------------------------------------------------- health


def test_health_window_open(window_a, app, clock):
    assert window_a.get("/api/health").json()["window_open"] is True  # A was bound at setup
    b = other_client(app)
    status(b, TAB_B)
    window_a.post("/api/app/closing", headers=hdr(TAB_A))
    assert window_a.get("/api/health").json()["window_open"] is True  # B is still open
    b.post("/api/app/closing", headers=hdr(TAB_B))
    assert window_a.get("/api/health").json()["window_open"] is False
    status(b, TAB_B)
    clock.advance(PRESENCE_TTL + 1)
    assert window_a.get("/api/health").json()["window_open"] is False


def test_health_before_any_window(client):
    assert client.get("/api/health").json()["window_open"] is False


# ------------------------------------------------------------ persisted limiter


def test_wrong_tries_survive_a_restart(tmp_path, fake_plaid, clock):
    def start():
        return SessionClient(create_app(make_settings(tmp_path), fake_plaid), base_url=BASE_URL, headers=CSRF)

    with start() as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        c.post("/api/auth/lock")
        for _ in range(2):
            assert c.post("/api/auth/unlock", json={"password": WRONG}).status_code == 401
    limiter_file = tmp_path / "data" / "limiter.json"
    assert json.loads(limiter_file.read_text(encoding="utf-8"))["failures"] == 2
    with start() as c:
        assert status(c)["tries_left"] == 3
        for left in (2, 1):
            assert c.post("/api/auth/unlock", json={"password": WRONG}).json()["tries_left"] == left
        assert c.post("/api/auth/unlock", json={"password": WRONG}).status_code == 429
    with start() as c:
        body = status(c)
        assert body["tries_left"] == 0 and 0 < body["retry_after"] <= 30
        assert c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 429
        clock.advance(31)
        assert c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
        assert not limiter_file.exists()
        assert status(c)["tries_left"] == 5


def test_limiter_file_is_untrusted(tmp_path, clock):
    path = tmp_path / "limiter.json"
    for bad in ("not json", "[]", '{"version": 2, "failures": 9, "locked_until": 0}',
                '{"version": 1, "failures": true, "locked_until": 0}',
                '{"version": 1, "failures": -3, "locked_until": 0}',
                '{"version": 1, "failures": 7, "locked_until": "soon"}',
                '{"version": 1, "failures": 7, "locked_until": NaN}'):
        path.write_text(bad, encoding="utf-8")
        limiter = RateLimiter(path)
        assert limiter.failures == 0 and limiter.retry_after() is None, bad
    # A wait far in the future (clock moved, or a tampered file) is capped at the lockout
    # that count gives: 7 failures -> 120 s.
    path.write_text(json.dumps({"version": 1, "failures": 7, "locked_until": time.time() + 10**9}), encoding="utf-8")
    limiter = RateLimiter(path)
    assert limiter.failures == 7 and limiter.retry_after() == 120
    path.write_text(json.dumps({"version": 1, "failures": 10**9, "locked_until": 0}), encoding="utf-8")
    assert RateLimiter(path).failures == RateLimiter.MAX_STORED_FAILURES
    # without a vault folder nothing is written
    missing = tmp_path / "nope" / "limiter.json"
    limiter = RateLimiter(missing)
    limiter.record_failure()
    assert not missing.exists() and limiter.tries_left() == 4


# ------------------------------------------------------------ review fixes (flood, race, silence)


def test_eviction_spares_the_session_window_and_pending_closes(clock):
    p = Presence()
    p.seen(TAB_A, keep=TAB_A)  # the session's window, oldest
    p.seen(TAB_B)
    p.request_close(TAB_B)  # another window with a close pending
    for i in range(MAX_TABS * 2):
        p.seen(f"evil-{i:08d}", keep=TAB_A)
    assert len(p) == MAX_TABS
    assert p.present(TAB_A) and p.closing(TAB_B) and p.last_seen(TAB_B) is not None
    # oldest other id dropped first
    assert p.last_seen("evil-00000000") is None and p.last_seen(f"evil-{MAX_TABS * 2 - 1:08d}") is not None


def test_eviction_never_drops_the_newcomer_when_it_is_the_session_window(clock, monkeypatch):
    monkeypatch.setattr(presence_mod, "MAX_TABS", 2)
    p = Presence()
    p.seen(TAB_A)
    p.request_close(TAB_A)
    p.seen(TAB_B)
    p.request_close(TAB_B)
    p.seen(TAB_C)  # only protected ids to drop: the newcomer isn't tracked
    assert p.last_seen(TAB_C) is None and p.closing(TAB_A) and p.closing(TAB_B)
    p.seen(TAB_C, keep=TAB_C)  # ... unless it holds the session
    assert p.present(TAB_C) and p.closing(TAB_A) and p.closing(TAB_B)


def test_a_flood_of_tab_ids_cannot_cancel_a_close_lock(window_a, app, clock, state, vault):
    assert window_a.post("/api/app/closing", headers=hdr(TAB_A)).status_code == 204
    attacker = other_client(app)
    for i in range(MAX_TABS + 8):
        status(attacker, f"evil-{i:08d}")
    assert state.presence.closing(TAB_A)
    clock.advance(presence_mod.CLOSE_GRACE + 1)
    app_routes.finish_due_closes(state)
    assert not vault.unlocked and vault.lock_reason == "closed"


def test_a_flood_of_tab_ids_cannot_hide_the_open_window(window_a, app, clock, state):
    attacker = other_client(app)
    for i in range(MAX_TABS + 8):
        status(attacker, f"evil-{i:08d}")
    assert state.presence.present(TAB_A)
    assert status(other_client(app), TAB_B)["open_elsewhere"] is True
    assert alive(other_client(app), TAB_C).json()["open_elsewhere"] is True


def test_request_close_ignores_a_window_heard_from_after_the_beacon(clock):
    p = Presence()
    p.seen(TAB_A)
    arrived = clock()
    clock.advance(1)
    p.seen(TAB_A)  # the reloaded page's status overtook the old page's beacon
    assert p.request_close(TAB_A, arrived) is False and not p.closing(TAB_A)
    assert p.request_close(TAB_A, clock()) is True and p.closing(TAB_A)  # same instant: closes


def test_a_status_handled_while_closing_waits_cancels_the_close(window_a, clock, state, vault, monkeypatch):
    """Reload: the old page's beacon waits for the vault lock while the new page's status
    lands; the late beacon must not start a close."""
    real_bound_tab = state.vault.bound_tab

    def slow_bound_tab():
        clock.advance(1)
        state.presence.seen(TAB_A, keep=TAB_A)  # the new page's status, meanwhile
        return real_bound_tab()

    monkeypatch.setattr(state.vault, "bound_tab", slow_bound_tab)
    assert window_a.post("/api/app/closing", headers=hdr(TAB_A)).status_code == 204
    monkeypatch.setattr(state.vault, "bound_tab", real_bound_tab)
    assert not state.presence.closing(TAB_A)
    clock.advance(presence_mod.CLOSE_GRACE + 1)
    app_routes.finish_due_closes(state)
    assert app_routes.finish_close(state, TAB_A) is False and vault.unlocked


@pytest.fixture
def packaged(tmp_path, fake_plaid, clock):
    """A packaged start (exit_when_unused > 0) with window A unlocked."""
    application = create_app(make_settings(tmp_path, exit_when_unused_seconds=600), fake_plaid)
    with SessionClient(application, base_url=BASE_URL, headers=CSRF) as c:
        r = c.post("/api/auth/setup", json={"password": PASSWORD}, headers=hdr(TAB_A))
        assert r.status_code == 200, r.text
        yield application, c


def test_packaged_start_locks_when_the_session_window_goes_silent(packaged, clock):
    application, c = packaged
    state_ = application.state.fintrack
    clock.advance(PRESENCE_TTL)
    assert app_routes.finish_silent_window(state_) is False and state_.vault.unlocked
    alive(c, TAB_A)  # heartbeats keep it open
    clock.advance(PRESENCE_TTL)
    assert app_routes.finish_silent_window(state_) is False and state_.vault.unlocked
    # other windows' heartbeats don't count for the session's window
    alive(other_client(application), TAB_B)
    clock.advance(1)
    assert app_routes.finish_silent_window(state_) is True
    assert not state_.vault.unlocked and state_.vault.lock_reason == "closed"
    assert state_.presence.last_seen(TAB_A) is None
    assert app_routes.finish_silent_window(state_) is False  # nothing left to lock


def test_owner_run_py_start_never_locks_a_silent_window(window_a, clock, state, vault):
    assert state.settings.exit_when_unused_seconds == 0
    clock.advance(PRESENCE_TTL * 3)  # a throttled background tab (still under the idle lock)
    assert app_routes.finish_silent_window(state) is False and vault.unlocked
