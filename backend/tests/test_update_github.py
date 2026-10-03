"""Updates from GitHub Releases (Iron Owl 2.0.0): the update source, the GitHub key set, the
check and download (network mocked: these tests never contact GitHub), the daily schedule,
the Settings switch and the status fields."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import random
import re
from pathlib import Path

import pytest

from app import update_keys
from app.updates import github, package as pkg
from app.updates.source import decide, read_release, valid_repo
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient
from tests.update_helpers import (
    CURRENT,
    Signer,
    app_entries,
    build_app,
    make_install_root,
    package_bytes,
    release_bytes,
    zip_bytes,
)

REPO = "iron-owl-app/Iron-Owl"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
T0 = dt.datetime(2026, 10, 2, 12, 0, 0)


def asset_url(version: str = "1.5.0") -> str:
    return f"https://github.com/{REPO}/releases/download/v{version}/Iron-Owl-{version}.ftupdate"


# URLs with a user name are built from parts so the export leak check sees no address.
AT = "@"
CDN_URL = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc?sig=x"


# ------------------------------------------------------------------ a fake network


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.pos = 0
        self.closed = False

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def read(self, n: int) -> bytes:
        block = self.body[self.pos:self.pos + n]
        self.pos += len(block)
        return block

    def close(self) -> None:
        self.closed = True


class FakeNet:
    """url -> response (or an exception, or a callable). Records every request."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[FakeResponse] = []

    def __call__(self, url: str, headers, timeout: float):
        self.calls.append((url, dict(headers)))
        assert timeout and timeout <= 60
        route = self.routes.get(url)
        if route is None:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(route, BaseException):
            raise route
        resp = route() if callable(route) else route
        if isinstance(resp, FakeResponse):
            resp = FakeResponse(resp.status, resp.body, resp.headers)
            self.responses.append(resp)
        return resp

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def release_json(version: str = "1.5.0", *, size: int, url: str | None = None, tag: str | None = None, **extra) -> bytes:
    name = f"Iron-Owl-{version}.ftupdate"
    data = {
        "tag_name": tag or f"v{version}", "draft": False, "prerelease": False,
        "assets": [
            {"id": 7, "name": f"Iron-Owl-Setup-{version}.exe", "size": 1234,
             "browser_download_url": f"https://github.com/{REPO}/releases/download/v{version}/Iron-Owl-Setup-{version}.exe"},
            {"id": 8, "name": name, "size": size, "browser_download_url": url or asset_url(version)},
        ],
        **extra,
    }
    return json.dumps(data).encode()


def net_for(data: bytes, version: str = "1.5.0", *, cdn: bool = True, **release_kw) -> FakeNet:
    routes = {API_URL: FakeResponse(200, release_json(version, size=len(data), **release_kw))}
    if cdn:
        routes[asset_url(version)] = FakeResponse(302, headers={"Location": CDN_URL})
        routes[CDN_URL] = FakeResponse(200, data, {"Content-Length": str(len(data))})
    else:
        routes[asset_url(version)] = FakeResponse(200, data)
    return FakeNet(routes)


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def gh_signer(monkeypatch) -> Signer:
    s = Signer("test-gh1")
    monkeypatch.setattr(update_keys, "GITHUB_KEYS", s.trusted)
    return s


@pytest.fixture
def file_signer(monkeypatch) -> Signer:
    s = Signer("test-file1")
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", s.trusted)
    return s


def write_release(root: Path, version: str = CURRENT, **extra) -> None:
    path = root / "versions" / version / "release.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")


class Clock:
    def __init__(self, at: dt.datetime = T0) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at


def make_client(tmp_path, monkeypatch, fake_plaid, *, release: dict | None = None, net: FakeNet | None = None,
                unlock: bool = True, **settings):
    root = make_install_root(tmp_path)
    if release is None:
        release = {"update_source": "github", "update_repo": REPO}
    write_release(root, **release)
    app, exits = build_app(tmp_path, monkeypatch, fake_plaid, root, **settings)
    updater = app.state.fintrack.updater
    updater.transport = net if net is not None else FakeNet()
    updater.now = Clock()
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    c.__enter__()
    c.updater, c.root, c.data, c.exits = updater, root, tmp_path / "data", exits
    if unlock:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return c


@pytest.fixture
def gh(tmp_path, monkeypatch, fake_plaid, gh_signer):
    c = make_client(tmp_path, monkeypatch, fake_plaid)
    yield c
    c.__exit__(None, None, None)


def check_now(c) -> dict:
    r = c.post("/api/update/scan", json={"now": True})
    assert r.status_code == 200, r.text
    return r.json()


def leftovers(c) -> list[str]:
    folder = c.data / "updates"
    return sorted(p.name for p in folder.glob(".incoming-*")) if folder.exists() else []


# ------------------------------------------------------------------ the update source


@pytest.mark.parametrize(("release", "override", "expected"), [
    (None, None, ("file", None)),  # no release.json: the emailed-file flow, as before
    ({}, None, ("file", None)),  # every install before 2.0.0
    ({"update_source": "file"}, None, ("file", None)),
    ({"update_source": "github", "update_repo": REPO}, None, ("github", REPO)),
    ({"update_source": "github"}, None, ("off", None)),  # no repo from the build
    ({"update_source": "github", "update_repo": "not a repo"}, None, ("off", None)),
    ({"update_source": "gitlab"}, None, ("off", None)),
    ({"update_source": "github", "update_repo": REPO}, "off", ("off", None)),
    ({"update_source": "github", "update_repo": REPO}, "FILE", ("file", None)),
    ({"update_source": "file"}, "github", ("off", None)),  # the repo only comes from the build
    ({"update_source": "file", "update_repo": REPO}, " github ", ("github", REPO)),
    ({}, "sometimes", ("off", None)),  # an unknown value never turns the network on
    ({}, "", ("file", None)),
])
def test_decide_source(release, override, expected):
    assert decide(mode="enabled", release=release, override=override) == expected


@pytest.mark.parametrize("mode", ["git", "dev", "not_installed"])
def test_git_and_dev_copies_stay_off_whatever_the_build_says(mode):
    release = {"update_source": "github", "update_repo": REPO}
    assert decide(mode=mode, release=release, override="github") == ("off", None)


@pytest.mark.parametrize("repo", [
    "iron-owl-app/Iron-Owl", "a/b", "A1-b2/x.y_z-1", "o" * 39 + "/" + "n" * 100, "owner/.github",
])
def test_valid_repos(repo):
    assert valid_repo(repo)


@pytest.mark.parametrize("repo", [
    None, 5, "", "iron-owl", "iron-owl/", "/Iron-Owl", "a/b/c", "-owl/x", "owl-/x", "ow--l/x",
    "o" * 40 + "/x", "owner/" + "n" * 101, "owner/.", "owner/..", "owner/x.git", "own er/x",
    "owner/x?y", "owner/x#y", "owner/x%2e", "own_er/x", "owner/x\n", "ówner/x", "owner/x/../y",
])
def test_invalid_repos(repo):
    assert not valid_repo(repo)


def test_read_release_is_defensive(tmp_path):
    assert read_release(tmp_path) is None
    (tmp_path / "release.json").write_bytes(b"[1, 2]")
    assert read_release(tmp_path) is None
    (tmp_path / "release.json").write_bytes(b"{" + b" " * 5000 + b"}")
    assert read_release(tmp_path) is None
    (tmp_path / "release.json").write_bytes(b"\xff\xfe")
    assert read_release(tmp_path) is None
    (tmp_path / "release.json").write_text(json.dumps({"update_source": "github"}), encoding="utf-8")
    assert read_release(tmp_path) == {"update_source": "github"}


def test_installed_copy_reads_its_source_from_release_json(tmp_path, monkeypatch, fake_plaid, gh_signer):
    c = make_client(tmp_path, monkeypatch, fake_plaid, unlock=False)
    try:
        assert c.updater.source == "github" and c.updater.repo == REPO
        assert c.updater.scanner is None  # GitHub copies never look in Downloads
    finally:
        c.__exit__(None, None, None)


def test_missing_source_keeps_the_emailed_flow(tmp_path, monkeypatch, fake_plaid, gh_signer):
    c = make_client(tmp_path, monkeypatch, fake_plaid, release={}, net=FakeNet())
    try:
        assert c.updater.source == "file" and c.updater.scanner is not None
        body = c.get("/api/update/status").json()
        assert body["source"] == "file" and body["auto_check"] is False
        assert body["last_check"] is None and body["check_error"] is None
        c.post("/api/update/scan", json={"now": True})
        c.updater.scan_tick(True)
        assert c.updater.transport.calls == []  # never online
        r = c.put("/api/update/auto-check", json={"on": False})
        assert r.status_code == 409 and r.json()["code"] == "disabled"
    finally:
        c.__exit__(None, None, None)


def test_update_source_off_in_the_settings_file(tmp_path, monkeypatch, fake_plaid, gh_signer):
    c = make_client(tmp_path, monkeypatch, fake_plaid, update_source="off")
    try:
        assert c.updater.source == "off" and c.updater.enabled and not c.updater.can_update
        body = c.get("/api/update/status").json()
        assert body["source"] == "off" and body["mode"] == "enabled" and body["offer"] is None
        for path, payload in (("/api/update/scan", {"now": True}), ("/api/update/dismiss", {"id": "0" * 16}),
                              ("/api/update/install", {"id": "0" * 16})):
            r = c.post(path, json=payload)
            assert r.status_code == 409 and r.json()["code"] == "disabled", path
        assert c.put("/api/update/auto-check", json={"on": True}).status_code == 409
        r = c.post("/api/update/file", files={"file": ("x.ftupdate", b"x" * 300, "application/octet-stream")})
        assert r.status_code == 409
        c.updater.scan_tick(True)
        assert c.updater.transport.calls == []
    finally:
        c.__exit__(None, None, None)


def test_git_copy_never_checks_github(tmp_path, monkeypatch, fake_plaid, gh_signer):
    root = make_install_root(tmp_path)
    write_release(root, update_source="github", update_repo=REPO)
    (root / "versions" / CURRENT / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    app, _ = build_app(tmp_path, monkeypatch, fake_plaid, root, update_source="github")
    updater = app.state.fintrack.updater
    assert updater.mode == "git" and updater.source == "off"
    updater.transport = FakeNet()
    assert updater.scan_tick(True) is False and updater.check_github(manual=True) is False
    assert updater.transport.calls == []


# ------------------------------------------------------------------ release.json in a payload


@pytest.mark.parametrize("extra", [
    {}, {"update_source": "file"}, {"update_source": "github", "update_repo": REPO},
])
def test_extract_accepts_the_optional_source_keys(tmp_path, gh_signer, extra):
    from tests.update_helpers import write_package

    entries = app_entries()
    entries[0] = ("release.json", release_bytes("1.5.0", **extra))
    path = write_package(tmp_path / "u.ftupdate", gh_signer, entries=entries)
    out = pkg.extract_payload(path, CURRENT, tmp_path / "versions", trusted_keys=gh_signer.trusted)
    assert json.loads((out.path / "release.json").read_text())["version"] == "1.5.0"


@pytest.mark.parametrize("extra", [
    {"update_source": "github"}, {"update_source": "github", "update_repo": "bad repo"},
    {"update_source": "file", "update_repo": REPO}, {"update_repo": REPO},
    {"update_source": "gitlab"}, {"update_source": None}, {"something_else": 1},
])
def test_extract_refuses_bad_source_keys(tmp_path, gh_signer, extra):
    from tests.update_helpers import write_package

    entries = app_entries()
    entries[0] = ("release.json", release_bytes("1.5.0", **extra))
    path = write_package(tmp_path / "u.ftupdate", gh_signer, entries=entries)
    with pytest.raises(pkg.ExtractError):
        pkg.extract_payload(path, CURRENT, tmp_path / "versions", trusted_keys=gh_signer.trusted)
    assert not (tmp_path / "versions" / "1.5.0").exists()


def test_optional_release_keys_constant():
    assert pkg.OPTIONAL_RELEASE_KEYS == {"update_source", "update_repo"}
    assert not pkg.OPTIONAL_RELEASE_KEYS & pkg.RELEASE_KEYS


# ------------------------------------------------------------------ the key sets


def test_committed_github_keys_are_real_ids_or_empty():
    real = importlib.reload(update_keys)
    assert len(real.GITHUB_KEYS) <= real.MAX_TRUSTED_KEYS
    for key_id in real.GITHUB_KEYS:
        assert re.fullmatch(real.GITHUB_KEY_ID_PATTERN, key_id), key_id
        assert not key_id.startswith(real.TEST_KEY_PREFIX)
    assert not set(real.GITHUB_KEYS) & set(real.TRUSTED_KEYS)
    assert not set(real.GITHUB_KEYS.values()) & set(real.TRUSTED_KEYS.values())


def test_sources_never_share_keys(monkeypatch, gh_signer, file_signer):
    assert update_keys.keys_for("github") == gh_signer.trusted
    assert update_keys.keys_for("file") == file_signer.trusted
    assert update_keys.keys_for("off") == {} and update_keys.keys_for("other") == {}
    # a file key pasted into the GitHub set by mistake is not trusted for GitHub
    monkeypatch.setattr(update_keys, "GITHUB_KEYS", {**gh_signer.trusted, "io-2026z": file_signer.public_b64})
    assert update_keys.keys_for("github") == gh_signer.trusted
    monkeypatch.setattr(update_keys, "GITHUB_KEYS", dict(file_signer.trusted))
    assert update_keys.keys_for("github") == {}


def test_store_check_errors_match_the_words():
    from app.updates.store import CHECK_ERRORS

    assert CHECK_ERRORS == github.ERROR_CODES
    for text in github.ERROR_TEXT.values():
        assert "FT-" not in text and "http" not in text.lower() and "{" not in text
        assert "Iron Owl" in text or "GitHub" in text


# ------------------------------------------------------------------ addresses


@pytest.mark.parametrize("url", [
    "https://api.github.com/repos/a/b/releases/latest", "https://github.com/a/b/releases/download/v1.0.0/x",
    "https://objects.githubusercontent.com/x?y=1", "https://release-assets.githubusercontent.com/x",
    "https://GitHub.com/a", "https://github.com:443/a",
])
def test_allowed_urls(url):
    assert github.allowed_url(url)


@pytest.mark.parametrize("url", [
    "http://github.com/a", "ftp://github.com/a", "file:///C:/x", "https://evil.example/a",
    "https://github.com.evil.example/a", "https://evilgithub.com/a", "https://user" + AT + "github.com/a",
    "https://user:pw" + AT + "github.com/a", "https://github.com:8443/a", "https://github.com/a b",
    "https://github.com/a\r\nX: y", "//github.com/a", "github.com/a", "", None, "https://[::1]/a",
    "https://raw.githubusercontent.com/a", "https://github.com:0/a", "https://" + "a" * 5000,
])
def test_refused_urls(url):
    assert not github.allowed_url(url)


# ------------------------------------------------------------------ checking GitHub


def test_newer_release_is_downloaded_verified_and_offered(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    gh.updater.transport = net = net_for(data)
    body = check_now(gh)
    assert net.urls == [API_URL, asset_url(), CDN_URL]
    offer = body["offer"]
    assert offer["version"] == "1.5.0" and offer["source"] == "github"
    assert offer["file_name"] == "Iron-Owl-1.5.0.ftupdate" and offer["can_install"] is True
    assert body["banner"]["show"] is True and body["source"] == "github"
    assert body["last_check"] == "2026-10-02T12:00:00Z" and body["check_error"] is None
    assert body["auto_check"] is True
    assert leftovers(gh) == []
    # nothing about the user goes out: no cookies, no auth, a plain User-Agent
    for url, headers in net.calls:
        names = {k.lower() for k in headers}
        assert names <= {"accept", "user-agent", "x-github-api-version"}, names
        assert headers["User-Agent"] == "Iron-Owl-updater"
        assert "?" not in url or url == CDN_URL


def test_github_offer_installs_with_the_github_key(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    gh.updater.transport = net_for(data)
    offer = check_now(gh)["offer"]
    r = gh.post("/api/update/install", json={"id": offer["id"]})
    assert r.status_code == 202, r.text
    assert gh.updater._job.wait(30)
    assert gh.exits.codes == [75]
    assert (gh.root / "versions" / "1.5.0" / "release.json").is_file()


@pytest.mark.parametrize("version", ["1.4.0", "1.3.9", "0.9.0"])
def test_same_or_older_release_is_not_downloaded(gh, gh_signer, version):
    gh.updater.transport = net = net_for(package_bytes(gh_signer, version=version), version)
    body = check_now(gh)
    assert net.urls == [API_URL]
    assert body["offer"] is None and body["check_error"] is None and body["last_check"] is not None


def test_no_release_yet_is_not_an_error(gh):
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(404, b'{"message":"Not Found"}')})
    body = check_now(gh)
    assert net.urls == [API_URL] and body["check_error"] is None and body["offer"] is None


@pytest.mark.parametrize(("response", "code"), [
    (FakeResponse(403, b"{}"), "busy"),
    (FakeResponse(429, b"{}"), "busy"),
    (FakeResponse(502, b""), "offline"),
    (FakeResponse(418, b""), "bad_answer"),
    (OSError("C:\\Users\\user\\secret path"), "offline"),
    (TimeoutError("timed out"), "offline"),
])
def test_api_failures_become_plain_words(gh, response, code):
    gh.updater.transport = FakeNet({API_URL: response})
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT[code]
    assert "Users" not in json.dumps(body) and body["offer"] is None
    assert gh.updater.store.check_state()["error"] == code


@pytest.mark.parametrize("raw", [
    b"not json", b"[]", b'"x"', b"{}", b'{"tag_name": 5, "assets": []}', b'{"tag_name": "latest", "assets": []}',
    b'{"tag_name": "v1.5", "assets": []}', b'{"tag_name": "v1.5.0", "assets": {}}',
    b'{"tag_name": "v1.5.0", "assets": [], "x": NaN}', b"\xff\xfe{}", b"[" * 100000,
    json.dumps({"tag_name": "v1.5.0", "assets": [{"name": "Iron-Owl-1.5.0.ftupdate", "id": 1, "size": "big",
                                                  "browser_download_url": asset_url()}]}).encode(),
    json.dumps({"tag_name": "v1.5.0", "assets": [{"name": "Iron-Owl-1.5.0.ftupdate", "id": 1, "size": 500,
                                                  "browser_download_url": 7}]}).encode(),
], ids=lambda raw: f"{len(raw)}b")
def test_bad_api_answers_download_nothing(gh, raw):
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(200, raw)})
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["bad_answer"]
    assert net.urls == [API_URL] and body["offer"] is None


def test_api_answer_is_capped(gh):
    huge = b'{"tag_name": "v1.5.0", "assets": [], "body": "' + b"x" * (github.MAX_JSON_BYTES + 10) + b'"}'
    gh.updater.transport = FakeNet({API_URL: FakeResponse(200, huge)})
    assert check_now(gh)["check_error"] == github.ERROR_TEXT["bad_answer"]


@pytest.mark.parametrize("extra", [{"draft": True}, {"prerelease": True}])
def test_drafts_and_prereleases_are_ignored(gh, gh_signer, extra):
    data = package_bytes(gh_signer, version="1.5.0")
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(200, release_json(size=len(data), **extra))})
    body = check_now(gh)
    assert net.urls == [API_URL] and body["offer"] is None and body["check_error"] is None


def test_release_without_the_update_file_is_not_an_error(gh):
    raw = json.dumps({"tag_name": "v1.5.0", "assets": [{"name": "Iron-Owl-Setup-1.5.0.exe", "id": 1, "size": 9,
                                                         "browser_download_url": "https://github.com/x"}]}).encode()
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(200, raw)})
    body = check_now(gh)
    assert net.urls == [API_URL] and body["check_error"] is None and body["offer"] is None


def test_asset_for_another_version_is_ignored(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    raw = release_json("1.5.0", size=len(data)).replace(b"Iron-Owl-1.5.0.ftupdate", b"Iron-Owl-1.6.0.ftupdate")
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(200, raw)})
    assert check_now(gh)["offer"] is None and net.urls == [API_URL]


@pytest.mark.parametrize("url", [
    "http://github.com/iron-owl-app/Iron-Owl/releases/download/v1.5.0/Iron-Owl-1.5.0.ftupdate",
    "https://evil.example/iron-owl-app/Iron-Owl/releases/download/v1.5.0/Iron-Owl-1.5.0.ftupdate",
    "https://github.com/someone-else/Iron-Owl/releases/download/v1.5.0/Iron-Owl-1.5.0.ftupdate",
    "https://github.com/iron-owl-app/Iron-Owl/releases/download/v1.5.0/Iron-Owl-1.5.0.ftupdate?x=1",
    "https://objects.githubusercontent.com/iron-owl-app/Iron-Owl/releases/download/v1.5.0/Iron-Owl-1.5.0.ftupdate",
])
def test_asset_address_must_be_this_repos_release(gh, gh_signer, url):
    data = package_bytes(gh_signer, version="1.5.0")
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(200, release_json(size=len(data), url=url))})
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["bad_answer"] and net.urls == [API_URL]


@pytest.mark.parametrize("location", [
    "https://evil.example/file.ftupdate",
    "http://objects.githubusercontent.com/file",
    "https://objects.githubusercontent.com.evil.example/file",
    "https://user:pw" + AT + "objects.githubusercontent.com/file",
    "file:///C:/Windows/notepad.exe",
    "",
])
def test_redirect_to_a_bad_address_is_refused(gh, gh_signer, location):
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    net.routes[asset_url()] = FakeResponse(302, headers={"Location": location} if location else {})
    gh.updater.transport = net
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["bad_answer"] and body["offer"] is None
    assert net.urls == [API_URL, asset_url()]  # the bad address itself is never requested
    assert leftovers(gh) == []


def test_relative_redirect_is_resolved_and_checked(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    moved = f"https://github.com/{REPO}/moved/Iron-Owl-1.5.0.ftupdate"
    net.routes[asset_url()] = FakeResponse(301, headers={"Location": f"/{REPO}/moved/Iron-Owl-1.5.0.ftupdate"})
    net.routes[moved] = FakeResponse(200, data)
    gh.updater.transport = net
    assert check_now(gh)["offer"]["version"] == "1.5.0"


def test_too_many_redirects(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    loop = f"https://github.com/{REPO}/loop"
    net.routes[asset_url()] = FakeResponse(302, headers={"Location": loop})
    net.routes[loop] = FakeResponse(302, headers={"Location": loop})
    gh.updater.transport = net
    assert check_now(gh)["check_error"] == github.ERROR_TEXT["bad_answer"]
    assert len(net.calls) == 1 + 1 + github.MAX_REDIRECTS


def test_declared_size_over_the_cap_is_never_downloaded(gh, gh_signer):
    raw = release_json(size=pkg.MAX_PACKAGE_BYTES + 1)
    gh.updater.transport = net = FakeNet({API_URL: FakeResponse(200, raw)})
    assert check_now(gh)["check_error"] == github.ERROR_TEXT["too_large"] and net.urls == [API_URL]


def test_size_cap_is_enforced_while_streaming(gh, gh_signer, monkeypatch):
    data = package_bytes(gh_signer, version="1.5.0")
    monkeypatch.setattr(pkg, "MAX_PACKAGE_BYTES", len(data) + 10)
    net = net_for(data + b"\x00" * 100)  # declared size within the cap, the body runs past it
    raw = release_json(size=len(data) + 5)
    net.routes[API_URL] = FakeResponse(200, raw)
    net.routes[CDN_URL] = FakeResponse(200, data + b"\x00" * 100)  # no Content-Length
    gh.updater.transport = net
    body = check_now(gh)
    assert body["check_error"] in (github.ERROR_TEXT["too_large"], github.ERROR_TEXT["bad_answer"])
    assert body["offer"] is None and leftovers(gh) == []
    stream = net.responses[-1]
    assert stream.pos <= len(data) + 5 + github.READ_CHUNK and stream.closed


def test_content_length_over_the_cap_stops_at_once(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    net.routes[CDN_URL] = FakeResponse(200, data, {"Content-Length": str(pkg.MAX_PACKAGE_BYTES + 1)})
    gh.updater.transport = net
    assert check_now(gh)["check_error"] == github.ERROR_TEXT["too_large"]
    assert net.responses[-1].pos == 0


@pytest.mark.parametrize("length", ["\u00b2", "\u00b3", "\u00b9", "\u0661\u0662", "1e3", "-1", "+5", "0x10", "", "9" * 13])
def test_odd_content_length_is_a_plain_error(gh, gh_signer, length):
    """Digits int() refuses (e.g. a superscript two, which str.isdigit() accepts) or odd forms:
    a recorded "didn't look right", never a crash that repeats every few minutes."""
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    net.routes[CDN_URL] = FakeResponse(200, data, {"Content-Length": length})
    gh.updater.transport = net
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["bad_answer"] and body["offer"] is None
    assert gh.updater.check_due() is False  # recorded: the retry waits a few hours
    assert net.responses[-1].pos == 0 and leftovers(gh) == []


def test_content_length_with_spaces_is_fine(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    net.routes[CDN_URL] = FakeResponse(200, data, {"Content-Length": f" {len(data)} "})
    gh.updater.transport = net
    body = check_now(gh)
    assert body["check_error"] is None and body["offer"]["version"] == "1.5.0"


def test_unexpected_error_in_a_check_is_recorded_not_a_500(gh, gh_signer, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise ValueError("something odd with a secret-looking detail")

    monkeypatch.setattr(github, "latest_release", broken)
    gh.updater.transport = FakeNet()
    r = gh.post("/api/update/scan", json={"now": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["check_error"] == github.ERROR_TEXT["bad_answer"]
    assert gh.updater.check_due() is False
    assert "secret-looking" not in caplog.text and "secret-looking" not in r.text  # the type only
    assert gh.updater.scan_tick(True) is False  # the automatic tick doesn't retry at once either


@pytest.mark.parametrize("cut", [10, 200, -1])
def test_short_or_wrong_size_download_is_refused(gh, gh_signer, cut):
    data = package_bytes(gh_signer, version="1.5.0")
    net = net_for(data)
    net.routes[CDN_URL] = FakeResponse(200, data[:cut])
    gh.updater.transport = net
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["bad_answer"] and body["offer"] is None


def test_wrong_key_is_refused_before_anything_is_written(gh, gh_signer, monkeypatch):
    other = Signer("test-other")
    big = [*app_entries(), ("web/assets/big.png", random.Random(1).randbytes(3_000_000))]  # not compressible
    data = package_bytes(other, version="1.5.0", payload=zip_bytes(big))
    written: list[int] = []
    real_check = pkg.check_head

    def spy(prefix, size, keys=None):
        written.append(sum(p.stat().st_size for p in (gh.data / "updates").rglob("package.ftupdate")))
        return real_check(prefix, size, keys)

    monkeypatch.setattr(pkg, "check_head", spy)
    gh.updater.transport = net = net_for(data)
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["rejected"] and body["offer"] is None
    assert written == [0]  # the head was checked before a single byte reached the disk
    assert net.responses[-1].pos < len(data) // 2  # and the download stopped there
    assert leftovers(gh) == [] and not list((gh.data / "updates").rglob("*.ftupdate"))
    # the same file is not downloaded again
    gh.updater.now.at += dt.timedelta(minutes=5)
    gh.updater.transport = net2 = net_for(data)
    assert check_now(gh)["check_error"] == github.ERROR_TEXT["rejected"]
    assert net2.urls == [API_URL]


def test_file_key_is_not_trusted_for_github(gh, gh_signer, file_signer):
    data = package_bytes(file_signer, version="1.5.0")
    gh.updater.transport = net_for(data)
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["rejected"] and body["offer"] is None
    assert gh.exits.codes == []


def test_picked_file_signed_with_the_file_key_is_refused_in_github_mode(gh, gh_signer, file_signer):
    data = package_bytes(file_signer, version="1.5.0")
    r = gh.post("/api/update/file", files={"file": ("Iron-Owl-1.5.0.ftupdate", data, "application/octet-stream")})
    assert r.status_code == 200 and r.json()["result"] == "rejected" and r.json()["code"] == "FT-UPD-SIG"
    good = package_bytes(gh_signer, version="1.5.0")
    r = gh.post("/api/update/file", files={"file": ("Iron-Owl-1.5.0.ftupdate", good, "application/octet-stream")})
    assert r.json()["result"] == "ready"


def test_github_key_is_not_trusted_for_the_file_source(tmp_path, monkeypatch, fake_plaid, gh_signer, file_signer):
    c = make_client(tmp_path, monkeypatch, fake_plaid, release={})
    try:
        data = package_bytes(gh_signer, version="1.5.0")
        r = c.post("/api/update/file", files={"file": ("FinTrack-1.5.0.ftupdate", data, "application/octet-stream")})
        assert r.json()["result"] == "rejected" and r.json()["code"] == "FT-UPD-SIG"
    finally:
        c.__exit__(None, None, None)


def test_install_rechecks_with_github_keys_only(gh, gh_signer, monkeypatch):
    data = package_bytes(gh_signer, version="1.5.0")
    gh.updater.transport = net_for(data)
    offer = check_now(gh)["offer"]
    # the key is withdrawn before the install: the re-check refuses the file, nothing installs
    monkeypatch.setattr(update_keys, "GITHUB_KEYS", {})
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", gh_signer.trusted)  # even if the file set had it
    r = gh.post("/api/update/install", json={"id": offer["id"]})
    assert r.status_code == 202 and r.json()["state"] == "failed"
    assert gh.exits.codes == [] and not (gh.root / "versions" / "1.5.0").exists()


def test_tag_and_file_version_must_agree(gh, gh_signer):
    data = package_bytes(gh_signer, version="1.6.0")  # the release says 1.5.0
    gh.updater.transport = net_for(data, "1.5.0")
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["rejected"] and body["offer"] is None
    assert not list((gh.data / "updates").rglob("*.ftupdate"))


def test_empty_github_key_set_fails_safe(gh, monkeypatch):
    monkeypatch.setattr(update_keys, "GITHUB_KEYS", {})
    gh.updater.transport = net = FakeNet()
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["no_keys"]
    assert "can’t be checked yet" in body["check_error"]
    assert net.calls == [] and body["offer"] is None and body["last_check"] is None


def test_newer_offer_already_there_is_kept(gh, gh_signer):
    gh.updater.transport = net_for(package_bytes(gh_signer, version="1.6.0"), "1.6.0")
    assert check_now(gh)["offer"]["version"] == "1.6.0"
    gh.updater.now.at += dt.timedelta(minutes=5)
    gh.updater.transport = net = net_for(package_bytes(gh_signer, version="1.5.0"))
    assert check_now(gh)["offer"]["version"] == "1.6.0"
    assert net.urls == [API_URL]


# ------------------------------------------------------------------ the daily check and the switch


def test_daily_schedule(gh, gh_signer):
    up = gh.updater
    up.transport = net = FakeNet({API_URL: FakeResponse(404)})
    assert up.check_due() is True
    assert up.scan_tick(False) is False and net.calls == []  # locked: never online
    assert up.scan_tick(True) is True and len(net.calls) == 1
    up.now.at += dt.timedelta(hours=23, minutes=59)
    assert up.scan_tick(True) is False and len(net.calls) == 1
    gh.post("/api/update/scan", json={})  # the window's own scans don't add checks either
    assert len(net.calls) == 1
    up.now.at += dt.timedelta(minutes=1)
    assert up.scan_tick(True) is True and len(net.calls) == 2


def test_failed_check_is_retried_after_a_few_hours(gh):
    up = gh.updater
    up.transport = net = FakeNet({API_URL: OSError("down")})
    assert up.scan_tick(True) is False and len(net.calls) == 1
    up.now.at += dt.timedelta(hours=2, minutes=59)
    assert up.check_due() is False
    up.now.at += dt.timedelta(minutes=1)
    assert up.check_due() is True


def test_clock_moved_back_counts_as_due(gh):
    up = gh.updater
    up.transport = FakeNet({API_URL: FakeResponse(404)})
    up.scan_tick(True)
    up.now.at -= dt.timedelta(days=2)
    assert up.check_due() is True


def test_switch_off_stops_the_automatic_check_but_not_check_now(gh):
    up = gh.updater
    up.transport = net = FakeNet({API_URL: FakeResponse(404)})
    r = gh.put("/api/update/auto-check", json={"on": False})
    assert r.status_code == 200 and r.json()["auto_check"] is False
    assert up.check_due() is False and up.scan_tick(True) is False
    gh.post("/api/update/scan", json={})
    assert net.calls == []
    check_now(gh)
    assert len(net.calls) == 1
    # the switch survives a restart (data/updates/state.json)
    assert json.loads((gh.data / "updates" / "state.json").read_text())["check"]["auto"] is False
    r = gh.put("/api/update/auto-check", json={"on": True})
    assert r.json()["auto_check"] is True


def test_check_now_at_most_once_a_minute(gh):
    up = gh.updater
    up.transport = net = FakeNet({API_URL: FakeResponse(404)})
    check_now(gh)
    check_now(gh)
    assert len(net.calls) == 1
    up.now.at += dt.timedelta(seconds=60)
    check_now(gh)
    assert len(net.calls) == 2


def test_no_check_while_installing(gh, gh_signer):
    up = gh.updater
    up.transport = net = FakeNet({API_URL: FakeResponse(404)})

    class Running:
        running = True

    up._job = Running()
    assert up.check_github(manual=True) is False and net.calls == []
    up._job = None


@pytest.mark.parametrize("payload", [{"on": "yes"}, {"on": 1}, {}, {"on": True, "x": 1}, {"on": None}])
def test_auto_check_body_is_strict(gh, payload):
    assert gh.put("/api/update/auto-check", json=payload).status_code == 422


@pytest.mark.parametrize("payload", [{"now": "yes"}, {"now": 1}, {"x": 1}])
def test_scan_body_is_strict(gh, payload):
    assert gh.post("/api/update/scan", json=payload).status_code == 422


def test_auto_check_needs_a_session(tmp_path, monkeypatch, fake_plaid, gh_signer):
    c = make_client(tmp_path, monkeypatch, fake_plaid, unlock=False)
    try:
        assert c.put("/api/update/auto-check", json={"on": False}).status_code == 401
    finally:
        c.__exit__(None, None, None)


def test_status_fields_in_github_mode(gh):
    body = gh.get("/api/update/status").json()
    assert body["source"] == "github" and body["auto_check"] is True
    assert body["last_check"] is None and body["check_error"] is None
    assert REPO not in json.dumps(body)  # the repo is not needed by the window


def test_store_check_state_is_validated(tmp_path):
    from app.updates.store import UpdateStore

    store = UpdateStore(tmp_path)
    assert store.check_state() == {"auto": True, "last_check": None, "last_try": None, "error": None}
    store.dir.mkdir(parents=True)
    store.path.write_text(json.dumps({"version": 1, "check": {
        "auto": "no", "last_check": "../x", "last_try": 5, "error": "C:\\Users\\user"}}), encoding="utf-8")
    assert store.check_state() == {"auto": True, "last_check": None, "last_try": None, "error": None}
    with pytest.raises(ValueError):
        store.record_check(at="2026-10-02T12:00:00Z", error="anything")


def test_real_transport_follows_no_redirects_and_keeps_no_cookies():
    import urllib.request as ur

    handlers = github._opener().handlers
    assert not any(isinstance(h, (ur.HTTPRedirectHandler, ur.HTTPCookieProcessor)) for h in handlers)
    assert not any(isinstance(h, (ur.HTTPHandler, ur.FileHandler, ur.FTPHandler, ur.DataHandler)) for h in handlers)
    assert any(isinstance(h, ur.HTTPSHandler) for h in handlers)


# ------------------------------------------------------------------ overall time limits (M2)


class TrickleResponse:
    """Answers a few bytes per read, and the fake clock moves 10 s with each read. (Not a
    FakeResponse: FakeNet would hand out a plain copy of one.)"""

    def __init__(self, body: bytes, clock: list[float]) -> None:
        self.status, self.body, self.pos, self.closed, self.clock = 200, body, 0, False, clock

    def header(self, name: str) -> str | None:
        return None

    def read(self, n: int) -> bytes:
        self.clock[0] += 10.0
        block = self.body[self.pos:self.pos + min(n, 5)]
        self.pos += len(block)
        return block

    def close(self) -> None:
        self.closed = True


def test_latest_release_has_an_overall_deadline():
    now = [0.0]
    body = release_json(size=5000)
    resp = TrickleResponse(body, now)
    net = FakeNet({API_URL: lambda: resp})
    with pytest.raises(github.CheckFailed) as exc:
        github.latest_release(net, REPO, clock=lambda: now[0])
    assert exc.value.code == "offline"
    assert now[0] <= github.API_SECONDS + 20 and resp.pos < len(body) and resp.closed
    # the same trickle with time to spare is read in full
    now[0] = 0.0
    resp2 = TrickleResponse(body, now)
    net2 = FakeNet({API_URL: lambda: resp2})
    release = github.latest_release(net2, REPO, clock=lambda: now[0], deadline_seconds=10.0 * (len(body) // 5 + 3))
    assert release is not None and release.version == "1.5.0"


def test_slow_check_is_recorded_as_offline(gh, gh_signer, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(github.time, "monotonic", lambda: clock[0])
    resp = TrickleResponse(release_json(size=5000), clock)
    gh.updater.transport = FakeNet({API_URL: lambda: resp})
    body = check_now(gh)
    assert body["check_error"] == github.ERROR_TEXT["offline"] and body["offer"] is None


def test_real_transport_reads_what_has_arrived():
    class Body:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def read1(self, n: int) -> bytes:
            self.calls.append("read1")
            return b"x"

        def read(self, n: int) -> bytes:
            self.calls.append("read")
            return b"x" * n

    body = Body()
    assert github._UrllibResponse(200, {}, body).read(64) == b"x" and body.calls == ["read1"]

    class Plain:
        def read(self, n: int) -> bytes:
            return b"y" * n

    assert github._UrllibResponse(200, {}, Plain()).read(3) == b"yyy"
