"""tools/release: keygen and build_update against a throwaway fake repo (never the real one)."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from app.config import REPO_ROOT
from app.updates import package as pkg
from tests.update_helpers import sections

sys.path.insert(0, str(REPO_ROOT / "tools" / "release"))
import build_update as bu  # noqa: E402
import keygen  # noqa: E402

PASSPHRASE = "correct horse battery staple 42"
RELEASED_AT = "2026-09-28T12:00:00Z"
NOTES = """# FinTrack release notes

## 1.5.0
- A new home screen shows what needs you first.
- Budget uses simpler words: Planned, Spent and Left.

## 1.4.0
- Older notes.
"""


# ------------------------------------------------------------------ keygen


def test_keygen_writes_passphrase_protected_key(tmp_path):
    restricted = []
    dest = tmp_path / ".fintrack-release" / "signing.pem"
    key_id, public = keygen.generate(dest, PASSPHRASE, "ft-2099a", restrict=lambda p: restricted.append(p) or True)
    assert key_id == "ft-2099a" and restricted == [dest.parent]
    data = dest.read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in data
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(data, password=None)
    with pytest.raises(ValueError):
        keygen.load_private_key(dest, "wrong passphrase, long enough")
    key = keygen.load_private_key(dest, PASSPHRASE)
    assert keygen.public_key_b64(key) == public
    assert len(__import__("base64").b64decode(public)) == 32


def test_keygen_refuses_overwrite_short_passphrase_and_bad_id(tmp_path):
    dest = tmp_path / "signing.pem"
    keygen.generate(dest, PASSPHRASE, "ft-2099a", restrict=lambda p: True)
    before = dest.read_bytes()
    with pytest.raises(FileExistsError):
        keygen.generate(dest, PASSPHRASE, "ft-2026b", restrict=lambda p: True)
    assert dest.read_bytes() == before
    with pytest.raises(ValueError):
        keygen.generate(tmp_path / "b.pem", "fifteen chars!!", "ft-2099a", restrict=lambda p: True)
    with pytest.raises(ValueError):
        keygen.generate(tmp_path / "c.pem", PASSPHRASE, "test key", restrict=lambda p: True)
    assert not (tmp_path / "b.pem").exists() and not (tmp_path / "c.pem").exists()


def test_unencrypted_key_refused(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = tmp_path / "plain.pem"
    path.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(TypeError):
        keygen.load_private_key(path, PASSPHRASE)


def test_passphrase_rules():
    with pytest.raises(ValueError):
        keygen.check_passphrase("x" * 23)
    keygen.check_passphrase("x" * 24)
    made = {keygen.generate_passphrase() for _ in range(20)}
    assert len(made) == 20
    for p in made:
        assert len(p) == 32 and re.fullmatch(r"[A-Za-z0-9_-]{32}", p)
        keygen.check_passphrase(p)


class Asker:
    def __init__(self, *answers: str) -> None:
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


def test_keygen_main_generates_passphrase_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(keygen, "restrict_to_owner", lambda p: True)
    dest = tmp_path / "k" / "signing.pem"
    ask = Asker("")  # Enter: generate one
    assert keygen.main(["--out", str(dest), "--key-id", "ft-2099a"], ask=ask) == 0
    assert len(ask.prompts) == 1
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines()]
    generated = [line for line in lines if re.fullmatch(r"[A-Za-z0-9_-]{32}", line)]
    assert len(generated) == 1  # printed exactly once
    passphrase = generated[0]
    assert out.count(passphrase) == 1 and "password manager" in out
    assert passphrase.encode() not in dest.read_bytes()
    keygen.load_private_key(dest, passphrase)


def test_keygen_main_own_passphrase(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(keygen, "restrict_to_owner", lambda p: True)
    dest = tmp_path / "signing.pem"
    assert keygen.main(["--out", str(dest)], ask=Asker(PASSPHRASE, PASSPHRASE)) == 0
    assert PASSPHRASE not in capsys.readouterr().out  # a chosen one is never echoed
    keygen.load_private_key(dest, PASSPHRASE)
    for answers in ((PASSPHRASE, PASSPHRASE + "x"), ("too short but 20 char",)):
        other = tmp_path / f"o{len(answers)}.pem"
        assert keygen.main(["--out", str(other)], ask=Asker(*answers)) == 1
        assert not other.exists()


def test_default_key_path_outside_repo():
    path = keygen.default_key_path()
    assert path.name == "signing.pem" and path.parent.name == ".fintrack-release"
    assert REPO_ROOT not in path.parents
    assert not keygen.inside_repo(path)


def test_keygen_refuses_a_key_inside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    for dest in (repo / "signing.pem", repo / "sub" / "k.pem", repo / "sub" / ".." / "x.pem"):
        with pytest.raises(ValueError, match="inside the repository"):
            keygen.generate(dest, PASSPHRASE, "ft-2099a", restrict=lambda p: True, repo_root=repo)
        assert not dest.exists()
    if sys.platform == "win32":  # Windows paths ignore case
        assert keygen.inside_repo(Path(str(repo).upper()) / "a.pem", repo)
    assert not keygen.inside_repo(tmp_path / "repo-other" / "a.pem", repo)
    keygen.generate(tmp_path / "outside" / "k.pem", PASSPHRASE, "ft-2099a", restrict=lambda p: True, repo_root=repo)


def test_keygen_main_refuses_out_inside_the_repo(capsys, monkeypatch):
    monkeypatch.setattr(keygen, "restrict_to_owner", lambda p: (_ for _ in ()).throw(AssertionError("no")))
    dest = keygen.REPO_ROOT / "tools" / "release" / "never-written.pem"
    ask = Asker()  # asking for a passphrase would fail: refused before that
    assert keygen.main(["--out", str(dest)], ask=ask) == 1
    assert not dest.exists() and ask.prompts == []
    assert "inside the repository" in capsys.readouterr().err


# ------------------------------------------------------------------ fake repo


@pytest.fixture(scope="module")
def key_file(tmp_path_factory) -> tuple[Path, str]:
    dest = tmp_path_factory.mktemp("key") / "signing.pem"
    _, public = keygen.generate(dest, PASSPHRASE, "ft-2099a", restrict=lambda p: True)
    return dest, public


def w(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def make_repo(root: Path, public: str, *, keys: dict | None = None) -> Path:
    keys = keys if keys is not None else {"ft-2099a": public}
    w(root / "backend/app/__init__.py", "")
    w(root / "backend/app/main.py", "APP = 1\n")
    w(root / "backend/app/version.py", '"""v"""\n__version__ = "1.4.0"\n')
    w(root / "backend/app/migrations.py", "LATEST = 5\n")
    w(root / "backend/app/update_keys.py", f"TRUSTED_KEYS: dict[str, str] = {keys!r}\n")
    w(root / "backend/app/routers/__init__.py", "")
    w(root / "backend/app/updates/README.md", "# developer notes, not shipped")
    w(root / "backend/app/__pycache__/main.cpython-311.pyc", b"\x00junk")
    w(root / "backend/run_packaged.py", "print('run')\n")
    w(root / "backend/requirements.txt", "fastapi==0.141.1\n# comment\ncryptography==46.0.7\npytest==9.1.1\n")
    w(root / "backend/tests/test_x.py", "def test(): pass\n")
    w(root / "frontend/dist/index.html", "<!doctype html>")
    w(root / "frontend/dist/assets/index-Ab12.js", "console.log(1)")
    w(root / "frontend/dist/assets/index-Cd34.css", "body{}")
    w(root / "frontend/dist/favicon.ico", b"\x00\x00\x01\x00")
    w(root / "frontend/dist/manifest.webmanifest", "{}")
    w(root / "frontend/src/main.tsx", "secret source")
    w(root / "frontend/node_modules/x/index.js", "x")
    w(root / ".env", "PLAID_SECRET=do-not-ship")
    w(root / "data/fintrack.db", b"vault")
    w(root / "data/keyfile.json", "{}")
    w(root / "design/updates/README.md", "design")
    w(root / ".claude/settings.json", "{}")
    w(root / "RELEASE_NOTES.md", NOTES)
    return root


class FakeGit:
    """rev-parse / status / ls-files -v / ls-tree + a blob reader for a fake repo. HEAD is a
    snapshot of the files on the first git call: every file under backend/app (except
    __pycache__) plus the launcher, unless listed in ``untracked``; ``head`` overrides the
    committed bytes of a path, ``flags`` the ``ls-files -v`` tag, ``modes`` the tree mode."""

    def __init__(self, branch: str = "master", status: str = "", *, ignored: str = "!! backend/app/__pycache__/\n",
                 after_status: str | None = None, untracked: tuple[str, ...] = (),
                 head: dict[str, bytes] | None = None, flags: dict[str, str] | None = None,
                 modes: dict[str, str] | None = None) -> None:
        self.branch, self.status, self.ignored, self.after_status = branch, status, ignored, after_status
        self.untracked = set(untracked)
        self.head_overrides = dict(head or {})
        self.flags, self.modes = dict(flags or {}), dict(modes or {})
        self.repo: Path | None = None
        self.calls: list[list[str]] = []
        self._status_calls = 0
        self._head: dict[str, bytes] | None = None

    def _snapshot(self) -> dict[str, bytes]:
        if self._head is None:
            assert self.repo is not None
            files = [p for p in (self.repo / "backend" / "app").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
            if (self.repo / "backend" / "run_packaged.py").exists():
                files.append(self.repo / "backend" / "run_packaged.py")
            head = {p.relative_to(self.repo).as_posix(): p.read_bytes() for p in files}
            head = {k: v for k, v in head.items() if k not in self.untracked}
            head.update(self.head_overrides)
            self._head = head
        return self._head

    @staticmethod
    def blob_id(data: bytes) -> str:
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    def read_blobs(self, shas: list[str]) -> dict[str, bytes]:
        by_id = {self.blob_id(data): data for data in self._snapshot().values()}
        return {sha: by_id[sha] for sha in shas}

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:1] == ["rev-parse"]:
            self._snapshot()
            return self.branch + "\n"
        if args[:1] == ["status"] and "--ignored" in args:
            # like git: tracked changes under the listed paths show here too (the bump)
            changed = self.after_status if self._status_calls > 1 and self.after_status is not None else self.status
            return changed + self.ignored
        if args[:1] == ["status"]:
            self._status_calls += 1
            if self._status_calls > 1 and self.after_status is not None:
                return self.after_status
            return self.status
        if args[:1] == ["ls-files"]:
            assert args[:3] == ["ls-files", "-v", "-z"]
            names = sorted(self._snapshot())
            return "".join(f"{self.flags.get(n, 'H')} {n}\0" for n in names)
        if args[:1] == ["ls-tree"]:
            assert args[:4] == ["ls-tree", "-r", "-z", "HEAD"]
            out = []
            for name, data in sorted(self._snapshot().items()):
                mode = self.modes.get(name, "100644")
                kind = "commit" if mode == "160000" else "blob"
                out.append(f"{mode} {kind} {self.blob_id(data)}\t{name}\0")
            return "".join(out)
        return ""


class FakeRunner:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, cmd: list[str], cwd: Path) -> None:
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in " ".join(cmd):
            raise bu.BuildError(f"{self.fail_on} failed")


def cfg(repo: Path, key: Path, **kw) -> bu.BuildConfig:
    kw.setdefault("released_at", RELEASED_AT)
    return bu.BuildConfig(repo_root=repo, version=kw.pop("version", "1.5.0"), key_path=key, **kw)


def run_build(repo: Path, key: Path, *, git=None, runner=None, passphrase=PASSPHRASE, **kw) -> bu.BuildResult:
    git = git or FakeGit()
    if isinstance(git, FakeGit) and git.repo is None:
        git.repo = repo
    return bu.build(cfg(repo, key, **kw), git=git, blobs=git.read_blobs, runner=runner or FakeRunner(),
                    passphrase=lambda: passphrase)


EXPECTED_PAYLOAD = sorted([
    "release.json", "run_packaged.py", "backend/app/__init__.py", "backend/app/main.py",
    "backend/app/version.py", "backend/app/migrations.py", "backend/app/update_keys.py",
    "backend/app/routers/__init__.py", "web/index.html", "web/assets/index-Ab12.js",
    "web/assets/index-Cd34.css", "web/favicon.ico", "web/manifest.webmanifest",
])


# ------------------------------------------------------------------ build


def test_build_happy_path(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    runner = FakeRunner()
    result = run_build(repo, key, runner=runner)
    out = repo / "dist-release"
    assert result.package == out / "Iron-Owl-1.5.0.ftupdate"
    assert result.payload_names == EXPECTED_PAYLOAD
    # tests and the web build ran, in order; npm runs no package install scripts
    assert "pytest" in runner.calls[0]
    assert runner.calls[1:] == [["npm", "ci", "--ignore-scripts"], ["npm", "run", "build"]]
    # the installed copy (trusting ft-2099a) accepts it and it extracts
    v = pkg.verify_package(result.package, "1.4.0", trusted_keys={"ft-2099a": public})
    assert v.result == "ready" and v.manifest.version == "1.5.0" and v.manifest.schema_version == 5
    assert v.manifest.notes == ("A new home screen shows what needs you first.",
                                "Budget uses simpler words: Planned, Spent and Left.")
    ex = pkg.extract_payload(result.package, "1.4.0", tmp_path / "versions", trusted_keys={"ft-2099a": public})
    assert (ex.path / "backend/app/version.py").read_text() == '"""v"""\n__version__ = "1.5.0"\n'
    # outputs
    digest = hashlib.sha256(result.package.read_bytes()).hexdigest()
    assert (out / "Iron-Owl-1.5.0.ftupdate.sha256").read_text() == f"{digest}  Iron-Owl-1.5.0.ftupdate\n"
    email = (out / "EMAIL.txt").read_text(encoding="utf-8")
    assert email == ("Subject: Iron Owl update 1.5\n\nHere's an Iron Owl update. Save the attached file, "
                     "then open Iron Owl; it will offer to install it.\n")
    releases = json.loads((repo / "tools/release/releases.json").read_text())["releases"]
    assert releases[-1]["version"] == "1.5.0" and releases[-1]["sha256"] == digest
    assert releases[-1]["trusted_keys"] == {"ft-2099a": public}
    assert '__version__ = "1.5.0"' in (repo / "backend/app/version.py").read_text()
    # v2 container: not a zip; the manifest binds the ENCODED payload
    data = result.package.read_bytes()
    assert data.startswith(pkg.MAGIC) and not zipfile.is_zipfile(result.package)
    mb, sig, enc = sections(data)
    assert json.loads(mb)["schema"] == 2 and json.loads(mb) == result.manifest
    assert result.manifest["payload"]["sha256"] == hashlib.sha256(enc).hexdigest()
    assert result.manifest["payload"]["size"] == len(enc)
    assert sig == pkg.canonical_json(json.loads(sig))


GREETING = "Hi Sam,\n\nhere's a FinTrack update. Save the attached file, then open FinTrack."


def test_email_greeting_from_the_local_file(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    # a BOM and Windows line endings (Notepad) are fine; outer blank space is trimmed
    w(repo / "tools/release/email_greeting.local.txt",
      b"\xef\xbb\xbf\r\n  " + GREETING.replace("\n", "\r\n").encode("utf-8") + b"\r\n\r\n")
    result = run_build(repo, key)
    email = (result.package.parent / "EMAIL.txt").read_text(encoding="utf-8")
    assert email == f"Subject: Iron Owl update 1.5\n\n{GREETING}\n"


def test_email_greeting_blank_file_uses_the_generic_text(tmp_path):
    w(tmp_path / "tools/release/email_greeting.local.txt", " \r\n\n ")
    assert bu.read_email_text(tmp_path) == bu.EMAIL_TEXT
    assert bu.read_email_text(tmp_path / "no-such-repo") == bu.EMAIL_TEXT


@pytest.mark.parametrize("data,problem", [
    (b"x" * 1001, "too long"),
    ("é".encode("utf-8") * 1001, "too long"),
    (b"x" * 10_000, "too long"),
    (b"Hi \xff there", "UTF-8"),
    (b"Hi\tthere", "odd characters"),
    (b"Hi\x07there", "odd characters"),
    (b"Hi\rthere", "odd characters"),
    ("Hi ‮there".encode("utf-8"), "odd characters"),  # right-to-left override
    ("Hi​there".encode("utf-8"), "odd characters"),  # zero-width space
])
def test_odd_email_greeting_stops_the_build_early(tmp_path, key_file, data, problem):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "tools/release/email_greeting.local.txt", data)
    runner = FakeRunner()
    with pytest.raises(bu.BuildError, match=problem) as info:
        run_build(repo, key, runner=runner)
    message = str(info.value)
    assert "email_greeting.local.txt" in message and str(tmp_path) not in message
    assert "Hi" not in message  # never echoes the file's text
    assert runner.calls == []  # before the tests and the web build
    assert '__version__ = "1.4.0"' in (repo / "backend/app/version.py").read_text()
    assert not (repo / "dist-release").exists()


def test_email_greeting_folder_is_refused(tmp_path):
    (tmp_path / "tools/release/email_greeting.local.txt").mkdir(parents=True)
    with pytest.raises(bu.BuildError, match="not a plain file"):
        bu.read_email_text(tmp_path)


def test_build_is_deterministic(tmp_path, key_file):
    key, public = key_file
    a = run_build(make_repo(tmp_path / "a", public), key)
    b = run_build(make_repo(tmp_path / "b", public), key)
    assert a.package.read_bytes() == b.package.read_bytes()
    assert a.sha256 == b.sha256


def test_payload_is_whitelist_only(tmp_path, key_file):
    key, public = key_file
    result = run_build(make_repo(tmp_path / "repo", public), key)
    payload = pkg.decode_payload(sections(result.package.read_bytes())[2])
    import io

    with zipfile.ZipFile(io.BytesIO(payload)) as inner:
        names = sorted(inner.namelist())
        for info in inner.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
    assert names == EXPECTED_PAYLOAD
    for bad in (".env", "fintrack.db", "keyfile", "design", ".claude", "src/", "node_modules", "tests", ".pyc"):
        assert not any(bad in n for n in names), bad


@pytest.mark.parametrize("path,content", [
    ("backend/app/notes.txt", "x"),
    ("backend/app/.env", "SECRET=1"),
    ("backend/app/Bad-Name.py", "x"),
    ("frontend/dist/assets/index.js.map", "{}"),
    ("frontend/dist/data/fintrack.db", "x"),
    ("frontend/dist/CON.js", "x"),
])
def test_unexpected_files_fail_the_build(tmp_path, key_file, path, content):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / path, content)
    with pytest.raises(bu.BuildError):
        run_build(repo, key)
    assert '__version__ = "1.4.0"' in (repo / "backend/app/version.py").read_text()  # restored
    assert not (repo / "dist-release").exists()
    assert not (repo / "tools/release/releases.json").exists()


def test_missing_launcher_or_web_build_fails(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    (repo / "backend/run_packaged.py").unlink()
    with pytest.raises(bu.BuildError, match="run_packaged"):
        run_build(repo, key)
    repo2 = make_repo(tmp_path / "repo2", public)
    (repo2 / "frontend/dist/index.html").unlink()
    with pytest.raises(bu.BuildError, match="index.html"):
        run_build(repo2, key)


@pytest.mark.parametrize("git", [FakeGit(status=" M backend/app/main.py\n"), FakeGit(status="?? stray.py\n"),
                                 FakeGit(branch="feature")])
def test_refuses_dirty_tree_or_wrong_branch(tmp_path, key_file, git):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    runner = FakeRunner()
    with pytest.raises(bu.BuildError):
        run_build(repo, key, git=git, runner=runner)
    assert runner.calls == []  # nothing ran
    assert '__version__ = "1.4.0"' in (repo / "backend/app/version.py").read_text()


@pytest.mark.parametrize("notes", [
    "## 1.4.0\n- Other version only.\n",
    "## 1.5.0\n- We fixed the server so it restarts.\n- Fine.\n",
    "## 1.5.0\n- The port is now configurable.\n- Fine.\n",
    "## 1.5.0\n- Checksum checks are faster.\n- Fine.\n",
    "## 1.5.0\n- Shows error code FT-UPD-03 less.\n- Fine.\n",
    "## 1.5.0\n- " + "x" * 161 + "\n- Fine.\n",
    "## 1.5.0\n" + "".join(f"- Point {i}.\n" for i in range(7)),
    "## 1.5.0\nSome paragraph instead of bullets.\n- Fine.\n",
    "## 1.5.0\n\n## 1.4.0\n- Old.\n",
])
def test_refuses_bad_release_notes(tmp_path, key_file, notes):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "RELEASE_NOTES.md", notes)
    runner = FakeRunner()
    with pytest.raises(bu.BuildError):
        run_build(repo, key, runner=runner)
    assert runner.calls == []


def test_notes_words_she_may_see():
    for fine in ("Reports show where the money goes.", "Support for larger text.", "Import is faster."):
        assert not bu.BANNED_NOTE_WORDS.search(fine), fine


def test_one_note_warns_but_builds(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "RELEASE_NOTES.md", "## 1.5.0\n- Just one thing.\n")
    result = run_build(repo, key)
    assert result.warnings and "2 to 4" in result.warnings[0]


def test_wrong_passphrase_restores_version(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    with pytest.raises(bu.BuildError, match="signing key"):
        run_build(repo, key, passphrase="not the passphrase at all")
    assert '__version__ = "1.4.0"' in (repo / "backend/app/version.py").read_text()
    assert not (repo / "dist-release").exists()


def test_key_must_be_trusted_by_the_release(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public, keys={"ft-2025a": public[::-1]})
    with pytest.raises(bu.BuildError, match="TRUSTED_KEYS"):
        run_build(repo, key)


def test_failing_tests_stop_the_build(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    runner = FakeRunner(fail_on="pytest")
    with pytest.raises(bu.BuildError):
        run_build(repo, key, runner=runner)
    assert len(runner.calls) == 1
    assert '__version__ = "1.4.0"' in (repo / "backend/app/version.py").read_text()


def _second_release(repo: Path, version: str = "1.6.0") -> None:
    notes = (repo / "RELEASE_NOTES.md").read_text()
    w(repo / "RELEASE_NOTES.md", f"## {version}\n- Another thing.\n- And one more.\n\n" + notes)


def test_version_must_increase(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    run_build(repo, key)
    with pytest.raises(bu.BuildError, match="not newer"):
        run_build(repo, key)
    with pytest.raises(bu.BuildError):
        run_build(repo, key, version="1.3.0")


def test_second_release_self_verifies_with_previous_keys(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    run_build(repo, key)
    _second_release(repo)
    assert run_build(repo, key, version="1.6.0").package.name == "Iron-Owl-1.6.0.ftupdate"
    # rotating to a key the installed copy (1.6.0's keys) doesn't have must fail the self-check
    _second_release(repo, "1.7.0")
    other = tmp_path / "other.pem"
    _, other_public = keygen.generate(other, PASSPHRASE, "ft-2027a", restrict=lambda p: True)
    w(repo / "backend/app/update_keys.py", f"TRUSTED_KEYS: dict[str, str] = {{'ft-2027a': {other_public!r}}}\n")
    with pytest.raises(bu.BuildError, match="FT-UPD-SIG"):
        run_build(repo, other, version="1.7.0")


def test_app_update_refuses_changed_dependencies(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    run_build(repo, key)
    _second_release(repo)
    w(repo / "backend/requirements.txt", "fastapi==0.142.0\ncryptography==46.0.7\n")
    with pytest.raises(bu.BuildError, match="--kind full"):
        run_build(repo, key, version="1.6.0")


def test_full_update_bundles_site_packages(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)

    class PipRunner(FakeRunner):
        def __call__(self, cmd, cwd):
            super().__call__(cmd, cwd)
            if "pip" in cmd:
                target = Path(cmd[cmd.index("--target") + 1])
                w(target / "fastapi/__init__.py", "")
                w(target / "fastapi/data/thing.json", "{}")  # "data" is fine inside site-packages
                w(target / "_cffi_backend.cp311-win_amd64.pyd", b"MZ")

    runner = PipRunner()
    result = run_build(repo, key, runner=runner, kind="full")
    assert "site-packages/fastapi/__init__.py" in result.payload_names
    pip = next(c for c in runner.calls if "pip" in c)
    assert "--only-binary=:all:" in pip and "win_amd64" in pip and "3.11" in pip
    v = pkg.verify_package(result.package, "1.4.0", trusted_keys={"ft-2099a": public})
    assert v.manifest.kind == "full"


def test_full_update_installs_the_lock_with_make_lock(tmp_path, key_file):
    """With the lock, a full update's runtime comes from make_lock.py install-runtime (hash-checked
    wheels, plus plaid-python built from its locked sdist), the same step as build_package.ps1."""
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    lock = repo / "tools" / "release" / "requirements-lock.txt"
    w(lock, "# deps_id: 0123456789abcdef\n")

    class LockRunner(FakeRunner):
        def __call__(self, cmd, cwd):
            super().__call__(cmd, cwd)
            if "install-runtime" in cmd:
                w(Path(cmd[cmd.index("install-runtime") + 2]) / "plaid/__init__.py", "")

    runner = LockRunner()
    result = run_build(repo, key, runner=runner, kind="full")
    assert "site-packages/plaid/__init__.py" in result.payload_names
    (call,) = [c for c in runner.calls if "install-runtime" in c]
    assert Path(call[1]) == REPO_ROOT / "tools" / "release" / "make_lock.py"
    assert call[2:4] == ["install-runtime", str(lock)]
    assert call[5:] == ["--python-version", "3.11"]
    assert not any("pip" in c for c in runner.calls)  # no direct pip install around the lock


def test_size_gate(tmp_path, key_file, monkeypatch):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    monkeypatch.setattr(bu, "APP_SIZE_LIMIT", 100)
    with pytest.raises(bu.BuildError, match="10 MB"):
        run_build(repo, key)


def test_deps_id_normalization():
    a = bu.normalized_requirements("B==1\n# c\n\nA==2  # pin\n")
    b = bu.normalized_requirements("a==2\nb==1\n")
    assert a == b


def test_deps_id_is_the_installer_builds_id(tmp_path):
    """build_package.ps1 names runtimes/<deps_id> with tools/release/deps_id.py: an app
    update must carry exactly that id or the installed copy won't find its runtime."""
    import deps_id as deps_id_tool

    text = (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    w(tmp_path / "backend/requirements.txt", text)
    assert bu.compute_deps_id(tmp_path) == deps_id_tool.deps_id(text)
    w(tmp_path / "backend/requirements.txt", "-r other.txt\n")
    with pytest.raises(bu.BuildError, match="unsupported"):
        bu.compute_deps_id(tmp_path)


def test_commit_flag_commits_and_tags(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    git = FakeGit()
    run_build(repo, key, git=git, commit=True)
    assert ["commit", "-m", "Release 1.5.0"] in git.calls
    assert any(c[:2] == ["tag", "-a"] and "v1.5.0" in c for c in git.calls)


# ------------------------------------------------------------------ v2 file, source hygiene, path budget


def test_built_file_hides_zip_and_script_signatures(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "frontend/dist/index.html", '<!doctype html><script type="module" src="/assets/index-Ab12.js"></script>')
    data = run_build(repo, key).package.read_bytes()
    for needle in (b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b".js", b"<script", b"web/"):
        assert needle not in data, needle


def test_only_committed_app_files_ship(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "backend/app/local_only.py", "SECRET = 1\n")
    # e.g. hidden by .git/info/exclude: git reports it as ignored, so the build refuses
    git = FakeGit(ignored="!! backend/app/__pycache__/\n!! backend/app/local_only.py\n",
                  untracked=("backend/app/local_only.py",))
    runner = FakeRunner()
    with pytest.raises(bu.BuildError, match="Ignored or untracked"):
        run_build(repo, key, git=git, runner=runner)
    assert runner.calls == []
    # and even if git said nothing, a file git doesn't track is never collected
    git = _with_repo(repo, "backend/app/local_only.py")
    files = bu.collect_payload(repo, "app", committed=bu.committed_files(git, git.read_blobs))
    assert "backend/app/local_only.py" not in files and "backend/app/main.py" in files


def _with_repo(repo: Path, *untracked: str) -> FakeGit:
    git = FakeGit(untracked=untracked)
    git.repo = repo
    return git


@pytest.mark.parametrize("line", ["!! frontend/src/.local-notes.md", "?? frontend/public/new.png",
                                  "!! backend/app/x.pyc", "!! backend/app/data/",
                                  "!! frontend/vite.config.local.ts", "?? frontend/postcss.config.js",
                                  "!! frontend/node_modules.bak/", "!! frontend/dist-old/"])
def test_ignored_or_untracked_source_refuses_build(tmp_path, key_file, line):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    runner = FakeRunner()
    with pytest.raises(bu.BuildError):
        run_build(repo, key, git=FakeGit(ignored=line + "\n"), runner=runner)
    assert runner.calls == []


def test_pycache_is_the_only_ignored_thing_allowed(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    git = FakeGit(ignored="!! backend/app/__pycache__/\n!! backend/app/routers/__pycache__/\n")
    assert run_build(repo, key, git=git).package.exists()
    assert ["status", "--porcelain", "--ignored", "--", "backend/app", "backend/run_packaged.py",
            "frontend"] in git.calls
    git = FakeGit(ignored="!! backend/app/__pycache__/\n!! frontend/node_modules/\n!! frontend/dist/\n")
    assert run_build(make_repo(tmp_path / "repo2", public), key, git=git).package.exists()


@pytest.mark.parametrize("after", [" M frontend/package-lock.json\n", "?? backend/app/generated.py\n",
                                   " M backend/app/main.py\n"])
def test_build_steps_must_not_dirty_the_tree(tmp_path, key_file, after):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    with pytest.raises(bu.BuildError, match="after the build steps"):
        run_build(repo, key, git=FakeGit(after_status=after))
    assert '__version__ = "1.4.0"' in (repo / "backend/app/version.py").read_text()
    assert not (repo / "dist-release").exists()


def test_version_bump_is_the_one_allowed_change(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    assert run_build(repo, key, git=FakeGit(after_status=" M backend/app/version.py\n")).package.exists()


@pytest.fixture
def short_tmp():
    """A short folder: pytest's basetemp plus a 140-character name can pass 260 here."""
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="ftr"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_payload_path_budget(short_tmp, key_file):
    key, public = key_file
    repo = make_repo(short_tmp / "a", public)
    name = "assets/" + "a" * (bu.PAYLOAD_PATH_BUDGET - len("web/assets/") - len(".js")) + ".js"
    assert len("web/" + name) == bu.PAYLOAD_PATH_BUDGET
    w(repo / "frontend/dist" / name, "x")
    assert "web/" + name in run_build(repo, key).payload_names
    repo2 = make_repo(short_tmp / "b", public)
    w(repo2 / "frontend/dist" / ("assets/b" + name[len("assets/"):]), "x")
    with pytest.raises(bu.BuildError, match="140 characters"):
        run_build(repo2, key)


def test_no_passphrase_from_the_environment():
    with pytest.raises(SystemExit):
        bu.main(["--version", "1.5.0", "--passphrase-env", "X"])


# ------------------------------------------------------------------ committed blobs, index flags, env files


def _payload_file(result: bu.BuildResult, name: str) -> bytes:
    import io

    payload = pkg.decode_payload(sections(result.package.read_bytes())[2])
    with zipfile.ZipFile(io.BytesIO(payload)) as inner:
        return inner.read(name)


def test_payload_ships_committed_bytes_not_the_working_tree(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    # HEAD differs from the working tree without git status noticing (e.g. assume-unchanged
    # cleared later, or a smudge filter): the committed bytes are what ships
    git = FakeGit(head={"backend/app/main.py": b"APP = 'committed'\n",
                        "backend/run_packaged.py": b"print('committed launcher')\n"})
    result = run_build(repo, key, git=git)
    assert _payload_file(result, "backend/app/main.py") == b"APP = 'committed'\n"
    assert _payload_file(result, "run_packaged.py") == b"print('committed launcher')\n"
    # version.py: the committed file with only the version bumped
    assert _payload_file(result, "backend/app/version.py") == b'"""v"""\n__version__ = "1.5.0"\n'
    assert (repo / "backend/app/main.py").read_text() == "APP = 1\n"


@pytest.mark.parametrize("tag", ["S", "h", "s", "M", "?"])
def test_skip_worktree_or_assume_unchanged_refuses_build(tmp_path, key_file, tag):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    runner = FakeRunner()
    with pytest.raises(bu.BuildError, match="skip-worktree or assume-unchanged"):
        run_build(repo, key, git=FakeGit(flags={"backend/app/main.py": tag}), runner=runner)
    assert runner.calls == []


def test_index_flags_cover_the_whole_frontend(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    git = FakeGit()
    run_build(repo, key, git=git)
    checks = [c for c in git.calls if c[:2] == ["ls-files", "-v"]]
    assert checks and all(c[3:] == ["--", "backend/app", "backend/run_packaged.py", "backend/requirements.txt",
                                    "frontend"] for c in checks)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_committed_link_or_submodule_refused(tmp_path, key_file, mode):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    with pytest.raises(bu.BuildError, match="link or submodule"):
        run_build(repo, key, git=FakeGit(modes={"backend/app/main.py": mode}))


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production.local", ".ENV.development", "src/.env"])
def test_frontend_env_files_refuse_build(tmp_path, key_file, name):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "frontend" / name, "VITE_X=1\n")
    runner = FakeRunner()
    with pytest.raises(bu.BuildError, match="Env files under frontend"):
        run_build(repo, key, runner=runner)
    assert runner.calls == []


def test_frontend_env_files_in_node_modules_are_not_ours(tmp_path, key_file):
    key, public = key_file
    repo = make_repo(tmp_path / "repo", public)
    w(repo / "frontend/node_modules/some-pkg/.env", "X=1\n")
    assert run_build(repo, key).package.exists()


def test_parse_cat_file_batch():
    a, b = "a" * 40, "b" * 40
    out = f"{a} blob 3\n".encode() + b"x\ny\n" + f"{b} blob 0\n".encode() + b"\n"
    assert bu.parse_cat_file_batch(out, [a, b]) == {a: b"x\ny", b: b""}
    for bad in (out[:-1], out + b"z", f"{a} missing\n".encode()):
        with pytest.raises(bu.BuildError):
            bu.parse_cat_file_batch(bad, [a, b])


def test_real_git_blobs(tmp_path):
    """default_git + default_blob_reader against a throwaway real repository."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "g"
    w(repo / "backend/app/__init__.py", "")
    w(repo / "backend/app/bin.py", b"X = b'\\x00\\xff'\r\n# \xc3\xa9\n")
    w(repo / "backend/run_packaged.py", "print(1)\n")
    w(repo / "backend/other.py", "not shipped\n")

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args], check=True, capture_output=True)

    run("init", "-q")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
    w(repo / "backend/app/bin.py", "changed after commit\n")
    git = bu.default_git(repo)
    got = bu.committed_files(git, bu.default_blob_reader(repo))
    assert got == {
        "backend/app/__init__.py": b"",
        "backend/app/bin.py": b"X = b'\\x00\\xff'\r\n# \xc3\xa9\n",
        "backend/run_packaged.py": b"print(1)\n",
    }
    bu.check_index_flags(git)  # all plain H
    run("update-index", "--skip-worktree", "backend/app/bin.py")
    with pytest.raises(bu.BuildError, match="skip-worktree"):
        bu.check_index_flags(git)
    run("update-index", "--no-skip-worktree", "backend/app/bin.py")
    run("update-index", "--assume-unchanged", "backend/run_packaged.py")
    with pytest.raises(bu.BuildError, match="assume-unchanged"):
        bu.check_index_flags(git)


def test_version_bump_keeps_crlf_line_endings(tmp_path):
    """A Windows checkout (core.autocrlf=true) has CRLF in version.py: still found, and bumped
    without changing any line ending."""
    app_dir = tmp_path / "backend" / "app"
    app_dir.mkdir(parents=True)
    original = b'"""FinTrack\'s version."""\r\n__version__ = "1.4.0"\r\n'
    (app_dir / "version.py").write_bytes(original)
    assert bu.read_app_version(tmp_path) == "1.4.0"
    previous = bu.write_app_version(tmp_path, "1.4.1")
    assert previous.encode("utf-8") == original
    assert (app_dir / "version.py").read_bytes() == b'"""FinTrack\'s version."""\r\n__version__ = "1.4.1"\r\n'
    assert bu.bump_version_text('__version__ = "1.4.0"\n', "1.5.0") == '__version__ = "1.5.0"\n'


# ------------------------------------------------------------------ GitHub builds (Iron Owl 2.0.0)

REPO_NAME = "iron-owl-app/Iron-Owl"


@pytest.fixture(scope="module")
def github_key(tmp_path_factory) -> tuple[Path, str]:
    dest = tmp_path_factory.mktemp("ghkey") / "github-signing.pem"
    _, public = keygen.generate(dest, PASSPHRASE, "io-2026a", restrict=lambda p: True)
    return dest, public


def make_github_repo(root: Path, ft_public: str, io_keys: dict) -> Path:
    make_repo(root, ft_public)
    w(root / "backend/app/version.py", '"""v"""\n__version__ = "1.5.0"\n')
    w(root / "backend/app/update_keys.py",
      f"TRUSTED_KEYS: dict[str, str] = {{'ft-2099a': {ft_public!r}}}\nGITHUB_KEYS: dict[str, str] = {io_keys!r}\n")
    return root


def run_github(repo: Path, key: Path, *, git=None, sign_repo: str | None = None, **kw) -> bu.BuildResult:
    """Both GitHub steps, as the release workflow runs them: build without a key into an
    unsigned folder, then sign that folder (``sign_repo`` signs for another repository)."""
    kw.setdefault("source", "github")
    kw.setdefault("update_repo", REPO_NAME)
    kw.setdefault("tag", "v1.5.0")
    folder = repo.parent / f"{repo.name}-unsigned"
    passphrase = kw.pop("passphrase", PASSPHRASE)

    def no_key():
        raise AssertionError("step 1 must never ask for the passphrase")

    git = git or FakeGit()
    if isinstance(git, FakeGit) and git.repo is None:
        git.repo = repo
    runner = kw.pop("runner", None) or FakeRunner()
    bu.build(cfg(repo, key, unsigned_out=folder, **kw), git=git, blobs=git.read_blobs, runner=runner,
             passphrase=no_key)
    if sign_repo is not None:
        kw["update_repo"] = sign_repo
    return bu.sign_only(cfg(repo, key, **kw), folder, git=git, passphrase=lambda: passphrase)


def _release_json(result: bu.BuildResult) -> dict:
    return json.loads(_payload_file(result, "release.json"))


def test_github_build(tmp_path, key_file, github_key):
    ft_key, ft_public = key_file
    gh_key, gh_public = github_key
    repo = make_github_repo(tmp_path / "repo", ft_public, {"io-2026a": gh_public})
    result = run_github(repo, gh_key)
    out = repo / "dist-release"
    assert result.package == out / "Iron-Owl-1.5.0.ftupdate"
    assert (out / "Iron-Owl-1.5.0.ftupdate.sha256").exists()
    assert not (out / "EMAIL.txt").exists()
    assert not (repo / "tools/release/releases.json").exists()  # the emailed history is untouched
    assert _release_json(result) == {
        "version": "1.5.0", "schema_version": 5, "deps_id": result.manifest["deps_id"], "kind": "app",
        "python": "3.11", "bootstrap_version": 1, "update_source": "github", "update_repo": REPO_NAME,
    }
    # GitHub keys verify it; the emailed-update key doesn't
    assert pkg.verify_package(result.package, "1.4.0", trusted_keys={"io-2026a": gh_public}).result == "ready"
    with pytest.raises(pkg.UpdateRejected):
        pkg.verify_package(result.package, "1.4.0", trusted_keys={"ft-2099a": ft_public})
    assert json.loads(sections(result.package.read_bytes())[1])["key_id"] == "io-2026a"
    if not hasattr(pkg, "OPTIONAL_RELEASE_KEYS"):
        assert any("Trial extraction skipped" in x for x in result.warnings)
    assert '__version__ = "1.5.0"' in (repo / "backend/app/version.py").read_text()


def test_file_build_release_json_has_only_the_old_keys(tmp_path, key_file):
    """Installed versions before 2.0.0 refuse any release.json key they don't know."""
    key, public = key_file
    result = run_build(make_repo(tmp_path / "repo", public), key)
    assert set(_release_json(result)) == {"version", "schema_version", "deps_id", "kind", "python", "bootstrap_version"}


@pytest.mark.parametrize("kw,match", [
    ({"update_repo": None}, "update-repo"),
    ({"update_repo": "iron-owl"}, "update-repo"),
    ({"update_repo": "-bad/Iron-Owl"}, "update-repo"),
    ({"update_repo": "iron-owl-app/Iron-Owl.git"}, "update-repo"),
    ({"update_repo": "iron-owl-app/Iron-Owl; rm"}, "update-repo"),
    ({"tag": None}, "--tag v1.5.0"),
    ({"tag": "v1.5.1"}, "--tag v1.5.0"),
    ({"commit": True}, "--commit"),
])
def test_github_build_refuses_bad_options(tmp_path, key_file, github_key, kw, match):
    gh_key, gh_public = github_key
    repo = make_github_repo(tmp_path / "repo", key_file[1], {"io-2026a": gh_public})
    runner = FakeRunner()
    with pytest.raises(bu.BuildError, match=match):
        run_github(repo, gh_key, runner=runner, **kw)
    assert runner.calls == [] and not (repo / "dist-release").exists()


def test_github_build_needs_version_py_to_match_the_tag(tmp_path, key_file, github_key):
    gh_key, gh_public = github_key
    repo = make_github_repo(tmp_path / "repo", key_file[1], {"io-2026a": gh_public})
    w(repo / "RELEASE_NOTES.md", "## 1.6.0\n- One.\n- Two.\n\n" + NOTES)
    with pytest.raises(bu.BuildError, match="must match"):
        run_github(repo, gh_key, version="1.6.0", tag="v1.6.0")


def test_github_build_needs_head_at_the_tag(tmp_path, key_file, github_key):
    gh_key, gh_public = github_key
    repo = make_github_repo(tmp_path / "repo", key_file[1], {"io-2026a": gh_public})

    class TagElsewhere(FakeGit):
        def __call__(self, args):
            if args[:2] == ["rev-parse", "--verify"]:
                self.calls.append(args)
                return ("a" * 40 if args[2].startswith("HEAD") else "b" * 40) + "\n"
            return super().__call__(args)

    git = TagElsewhere()
    with pytest.raises(bu.BuildError, match="not the commit tagged v1.5.0"):
        run_github(repo, gh_key, git=git)
    assert ["rev-parse", "--verify", "refs/tags/v1.5.0^{commit}"] in git.calls


@pytest.mark.parametrize("keys,match", [
    ({}, "GITHUB_KEYS is empty"),
    ({"ft-2099a": "x"}, "don't belong to github builds"),
])
def test_github_build_needs_github_keys(tmp_path, key_file, github_key, keys, match):
    gh_key, _ = github_key
    repo = make_github_repo(tmp_path / "repo", key_file[1], keys)
    with pytest.raises(bu.BuildError, match=match):
        run_github(repo, gh_key)


def test_github_build_never_signs_with_the_emailed_key(tmp_path, key_file, github_key):
    ft_key, ft_public = key_file
    repo = make_github_repo(tmp_path / "repo", ft_public, {"io-2026a": github_key[1]})
    with pytest.raises(bu.BuildError, match="GITHUB_KEYS"):
        run_github(repo, ft_key)
    assert not (repo / "dist-release").exists()


# ------------------------------------------------------------------ GitHub: build, then sign


def _unsigned(tmp_path, key_file, github_key) -> tuple[Path, Path, Path]:
    """(repo, unsigned folder, GitHub key file) after step 1."""
    gh_key, gh_public = github_key
    repo = make_github_repo(tmp_path / "repo", key_file[1], {"io-2026a": gh_public})
    folder = tmp_path / "unsigned"
    git = FakeGit()
    git.repo = repo
    bu.build(cfg(repo, gh_key, source="github", update_repo=REPO_NAME, tag="v1.5.0", unsigned_out=folder),
             git=git, blobs=git.read_blobs, runner=FakeRunner(), passphrase=lambda: pytest.fail("no key in step 1"))
    return repo, folder, gh_key


def _sign(repo: Path, folder: Path, key: Path, **kw) -> bu.BuildResult:
    kw.setdefault("update_repo", REPO_NAME)
    kw.setdefault("tag", "v1.5.0")
    git = FakeGit()
    git.repo = repo
    return bu.sign_only(cfg(repo, key, source="github", **kw), folder, git=git, passphrase=lambda: PASSPHRASE)


def test_github_build_in_one_step_is_refused(tmp_path, key_file, github_key):
    """The key must never be present while tests and npm run: a GitHub build has two steps."""
    gh_key, gh_public = github_key
    repo = make_github_repo(tmp_path / "repo", key_file[1], {"io-2026a": gh_public})
    runner = FakeRunner()
    with pytest.raises(bu.BuildError, match="two steps"):
        bu.build(cfg(repo, gh_key, source="github", update_repo=REPO_NAME, tag="v1.5.0"), git=FakeGit(),
                 runner=runner, passphrase=lambda: pytest.fail("asked for the passphrase"))
    assert runner.calls == []
    key, public = key_file
    with pytest.raises(bu.BuildError, match="only for --source github"):
        run_build(make_repo(tmp_path / "file-repo", public), key, unsigned_out=tmp_path / "u")


def test_unsigned_step_writes_only_manifest_and_payload(tmp_path, key_file, github_key):
    repo, folder, _ = _unsigned(tmp_path, key_file, github_key)
    assert sorted(p.name for p in folder.iterdir()) == ["manifest.json", "payload.bin"]
    manifest = json.loads((folder / "manifest.json").read_bytes())
    assert manifest["version"] == "1.5.0"
    assert manifest["payload"]["sha256"] == hashlib.sha256((folder / "payload.bin").read_bytes()).hexdigest()
    assert not (repo / "dist-release").exists()
    # a non-empty folder is refused (nothing mixes with an older build)
    with pytest.raises(bu.BuildError, match="new or empty folder"):
        bu.build(cfg(repo, Path("unused"), source="github", update_repo=REPO_NAME, tag="v1.5.0", unsigned_out=folder),
                 git=FakeGit(), runner=FakeRunner())


def test_sign_only_runs_nothing_but_git(tmp_path, key_file, github_key, monkeypatch):
    repo, folder, gh_key = _unsigned(tmp_path, key_file, github_key)

    def no_programs(*a, **k):
        raise AssertionError("sign_only must not start a program")

    monkeypatch.setattr(bu.subprocess, "run", no_programs)
    git = FakeGit()
    git.repo = repo
    result = bu.sign_only(cfg(repo, gh_key, source="github", update_repo=REPO_NAME, tag="v1.5.0"), folder, git=git,
                          passphrase=lambda: PASSPHRASE)
    assert {c[0] for c in git.calls} <= {"status", "rev-parse", "ls-files"}  # the clean-checkout checks
    assert result.package == repo / "dist-release" / "Iron-Owl-1.5.0.ftupdate"
    assert pkg.verify_package(result.package, "1.4.0", trusted_keys={"io-2026a": github_key[1]}).result == "ready"
    assert "release.json" in result.payload_names


@pytest.mark.parametrize("tamper,match", [
    (lambda f: (f / "payload.bin").write_bytes((f / "payload.bin").read_bytes() + b"x"), "doesn't match its manifest"),
    (lambda f: (f / "extra.txt").write_text("x"), "only manifest.json and payload.bin"),
    (lambda f: (f / "payload.bin").unlink(), "only manifest.json and payload.bin"),
    (lambda f: (f / "manifest.json").write_bytes(b"{}"), "app's own checks"),
    (lambda f: (f / "manifest.json").write_bytes((f / "manifest.json").read_bytes() + b"\n"), "app's own checks|canonical"),
])
def test_sign_only_refuses_a_changed_unsigned_folder(tmp_path, key_file, github_key, tamper, match):
    repo, folder, gh_key = _unsigned(tmp_path, key_file, github_key)
    tamper(folder)
    with pytest.raises(bu.BuildError, match=match):
        _sign(repo, folder, gh_key)
    assert not (repo / "dist-release").exists()


def test_sign_only_checks_version_repository_and_source(tmp_path, key_file, github_key):
    repo, folder, gh_key = _unsigned(tmp_path, key_file, github_key)
    with pytest.raises(bu.BuildError, match="not for this version and repository"):
        _sign(repo, folder, gh_key, update_repo="someone/Else")
    w(repo / "backend/app/version.py", '"""v"""\n__version__ = "1.6.0"\n')
    with pytest.raises(bu.BuildError, match="must match"):
        _sign(repo, folder, gh_key)  # the checkout's version.py says 1.6.0, the tag 1.5.0
    with pytest.raises(bu.BuildError, match="is for 1.5.0, not 1.6.0"):
        _sign(repo, folder, gh_key, version="1.6.0", tag="v1.6.0")  # the folder is from another version
    with pytest.raises(bu.BuildError, match="only for --source github"):
        bu.sign_only(cfg(repo, gh_key), folder, git=FakeGit(), passphrase=lambda: PASSPHRASE)
    assert not (repo / "dist-release").exists()


def _repack(folder: Path, release_bytes: bytes) -> None:
    """Rewrite the unsigned folder with another release.json, consistently (manifest updated)."""
    import io as _io

    manifest = json.loads((folder / "manifest.json").read_bytes())
    payload = pkg.decode_payload((folder / "payload.bin").read_bytes())
    with zipfile.ZipFile(_io.BytesIO(payload)) as zf:
        entries = [(n, zf.read(n)) for n in zf.namelist() if n != "release.json"]
    entries.append(("release.json", release_bytes))
    encoded = pkg.encode_payload(bu.deterministic_zip(entries, compress=zipfile.ZIP_DEFLATED))
    manifest["payload"].update({"size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()})
    (folder / "payload.bin").write_bytes(encoded)
    (folder / "manifest.json").write_bytes(bu.canonical_json(manifest))


def test_sign_only_refuses_an_odd_release_json(tmp_path, key_file, github_key):
    repo, folder, gh_key = _unsigned(tmp_path, key_file, github_key)
    good = json.dumps({"version": "1.5.0", "update_source": "github", "update_repo": REPO_NAME}).encode()
    _repack(folder, good[:-1] + b" " * (pkg.MAX_RELEASE_JSON_BYTES + 1) + b"}")
    with pytest.raises(bu.BuildError, match="no readable release.json"):
        _sign(repo, folder, gh_key)
    _repack(folder, json.dumps({"version": "1.5.0", "update_source": "file"}).encode())
    with pytest.raises(bu.BuildError, match="not for this version and repository"):
        _sign(repo, folder, gh_key)
    assert not (repo / "dist-release").exists()


def test_sign_only_needs_head_at_the_tag(tmp_path, key_file, github_key):
    repo, folder, gh_key = _unsigned(tmp_path, key_file, github_key)

    class TagElsewhere(FakeGit):
        def __call__(self, args):
            if args[:2] == ["rev-parse", "--verify"]:
                return ("a" * 40 if args[2].startswith("HEAD") else "b" * 40) + "\n"
            return super().__call__(args)

    git = TagElsewhere()
    git.repo = repo
    with pytest.raises(bu.BuildError, match="not the commit tagged"):
        bu.sign_only(cfg(repo, gh_key, source="github", update_repo=REPO_NAME, tag="v1.5.0"), folder, git=git,
                     passphrase=lambda: PASSPHRASE)


def test_cli_steps_are_separate(tmp_path):
    with pytest.raises(SystemExit):
        bu.main(["--version", "1.5.0", "--unsigned-out", str(tmp_path / "a"), "--sign-only", str(tmp_path / "b")])


def test_signing_code_imports_only_the_standard_library_and_cryptography():
    """The signing job installs only cryptography (hash-checked): build_update.py must not
    need anything else."""
    import subprocess

    script = (
        "import sys; before = set(sys.modules); "
        f"sys.path.insert(0, {str(REPO_ROOT / 'tools' / 'release')!r}); import build_update; "
        "names = {m.split('.')[0] for m in set(sys.modules) - before}; "
        "std = set(sys.stdlib_module_names); "
        "print(sorted(n for n in names if n not in std and not n.startswith('_')))"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT))
    assert done.returncode == 0, done.stderr
    extra = set(eval(done.stdout.strip())) - {"cryptography", "cffi", "pycparser", "app", "deps_id", "keygen",
                                               "build_update"}
    assert extra == set(), extra


@pytest.mark.parametrize("value,ok", [
    ("iron-owl-app/Iron-Owl", True), ("a/b", True), ("A1-b2/x.y_z-1", True), ("o" * 39 + "/n", True),
    ("o" * 40 + "/n", False), ("-a/b", False), ("a-/b", False), ("a--b/c", False), ("a/.", False),
    ("a/..", False), ("a/b.git", False), ("a/B.GIT", False), ("a/" + "n" * 101, False), ("a", False),
    ("a/b/c", False), ("a/b c", False), ("a/b\n", False), ("", False), (None, False), ("ä/b", False),
])
def test_valid_repo(value, ok):
    assert bu.valid_repo(value) is ok


def test_passphrase_from_stdin():
    import io

    assert bu.read_passphrase_stdin(io.StringIO("secret phrase here\r\nnext")) == "secret phrase here"
    for bad in ("", "\n", "x" * 2000):
        with pytest.raises(bu.BuildError):
            bu.read_passphrase_stdin(io.StringIO(bad))
    with pytest.raises(SystemExit):  # still no way to pass it on the command line
        bu.main(["--version", "1.5.0", "--passphrase", "X"])


def test_keygen_github_key_ids_and_default_file(tmp_path, capsys, monkeypatch):
    assert keygen.default_key_path("io-2026a").name == "github-signing.pem"
    assert keygen.default_key_path("ft-2099a").name == "signing.pem"
    monkeypatch.setattr(keygen, "restrict_to_owner", lambda p: True)
    dest = tmp_path / "gh.pem"
    assert keygen.main(["--out", str(dest), "--key-id", "io-2026a"], ask=Asker(PASSPHRASE, PASSPHRASE)) == 0
    out = capsys.readouterr().out
    assert "GITHUB_KEYS" in out and "IRON_OWL_SIGNING_KEY" in out and "TRUSTED_KEYS" not in out
    with pytest.raises(ValueError):
        keygen.generate(tmp_path / "x.pem", PASSPHRASE, "gh-2026a", restrict=lambda p: True)
