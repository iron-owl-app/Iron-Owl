"""Build a signed Iron Owl update file: ``dist-release/Iron-Owl-X.Y.Z.ftupdate``.

    backend\\.venv\\Scripts\\python.exe tools\\release\\build_update.py --version 1.4.0 [--kind app|full]

(or ``tools\\release\\release.ps1 1.4.0``). Two sources (Iron Owl 2.0.0):

* ``--source file`` (default): the owner's emailed builds, signed with a ``ft-`` key from
  ``TRUSTED_KEYS``; release.json carries no source keys (installed versions before 2.0.0
  refuse unknown keys there).
* ``--source github --update-repo <owner>/<name> --tag vX.Y.Z``: the public build that
  ``.github/workflows/release.yml`` makes from a tag. Signed with an ``io-`` key from
  ``GITHUB_KEYS`` and self-verified against ``GITHUB_KEYS`` only; release.json gets
  ``update_source: "github"`` and ``update_repo``. HEAD must be that tag's commit and the
  version must equal version.py (nothing is bumped); releases.json is neither read nor written
  and no EMAIL.txt is made. ``--passphrase-stdin`` reads the key's passphrase from standard
  input (the workflow pipes it from a secret), never from a command line or the environment.

  A GitHub build always has two steps, so the signing key is never present while tests, npm
  or the web build run (they run third-party code):

  1. ``--unsigned-out <folder>``: everything up to the manifest (steps 1-7 below and the
     manifest of step 8), written as ``manifest.json`` + ``payload.bin`` (the encoded
     payload). No key, no passphrase.
  2. ``--sign-only <folder>``: from a clean checkout of the tag, with only the standard
     library and ``cryptography``: checks the folder (the app's own manifest parser, payload
     size and SHA-256, release.json's version and source), signs, self-verifies with
     GITHUB_KEYS and a trial extraction, writes the .ftupdate. Runs no other program but git.

Steps, stopping at the first problem:

1.  clean ``master``: ``git status --porcelain`` empty; ``git status --porcelain --ignored
    -- backend/app backend/run_packaged.py frontend`` shows nothing but ``__pycache__``,
    ``frontend/node_modules/`` and ``frontend/dist/`` (no ignored or untracked file can feed
    the build); no env-style file (a name starting with a dot followed by "env") anywhere
    under frontend -- Vite would bake it into the bundle, tracked or not; and ``git ls-files
    -v`` shows every file under the payload sources as plain ``H`` (a file marked
    skip-worktree or assume-unchanged hides its edits from ``git status``);
2.  RELEASE_NOTES.md has a ``## X.Y.Z`` section of plain bullet points (1-6, 2-4 is best,
    <= 160 characters, none of the words the user must never see: server, port, checksum...);
3.  the version is newer than the last release and not older than version.py; bump
    ``backend/app/version.py`` (restored if anything below fails);
4.  backend tests (pytest), then ``npm ci --ignore-scripts`` + ``npm run build``; afterwards
    the tree must still be clean apart from the version.py bump;
5.  deps_id = hash of the normalized runtime requirements; an ``app`` update must keep the
    previous release's deps_id (else build ``--kind full``, which bundles site-packages);
6.  payload = ONLY release.json, run_packaged.py and backend/app/**/*.py as COMMITTED at
    HEAD (blobs read with ``git ls-tree`` + ``git cat-file``, never the working tree; only
    version.py gets the version bump), frontend/dist as web/** (and site-packages/** for
    full), each name
    checked with the same path rules and whitelist the app uses to verify and kept to 140
    characters (so versions/<v>/<name> fits Windows paths); anything else is an error,
    never silently shipped;
7.  deterministic payload.zip (sorted, fixed timestamps and attributes), then encoded with
    the fixed, public, not-a-secret transform (``package.encode_payload``) so the file shows
    no zip or script signatures to mail filters;
8.  manifest.json (schema 2; payload size + SHA-256 of the ENCODED bytes), signed with the
    passphrase-protected key (asked with getpass) whose public key must be in
    backend/app/update_keys.py;
9.  the v2 container: magic + length-prefixed manifest, signature, encoded payload;
10. self-verify with the PREVIOUS release's trusted keys (what the installed copy has) and a trial
    extraction; size gate (app > 10 MB fails: email limits);
11. writes Iron-Owl-X.Y.Z.ftupdate, its .sha256, EMAIL.txt (its text comes from the untracked
    tools/release/email_greeting.local.txt if present, else a generic one); appends
    tools/release/releases.json.

Then commit "Release X.Y.Z" and tag (``--commit`` does both).
"""
from __future__ import annotations

import argparse
import ast
import base64
import datetime as dt
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL_REPO_ROOT = HERE.parents[1]
if str(TOOL_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(TOOL_REPO_ROOT / "backend"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import deps_id as deps_id_tool  # noqa: E402 - the runtime id the installer build uses too
import keygen  # noqa: E402
from app.updates import package as pkg  # noqa: E402
from app.updates import semver  # noqa: E402
from app.updates.requirements import Environment  # noqa: E402

MB = 1024 * 1024
APP_SIZE_LIMIT = 10 * MB
FULL_SIZE_WARNING = 20 * MB
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
# versions/<X.Y.Z>/<name> under the install root must stay well inside Windows' 260
# characters even where long paths aren't used (the app also extracts via \\?\ paths).
PAYLOAD_PATH_BUDGET = 140
# Everything the build reads: no ignored/untracked files here (bar the allowed build outputs).
SOURCE_DIRS = ("backend/app", "backend/run_packaged.py", "frontend")
ALLOWED_IGNORED = frozenset({"frontend/node_modules", "frontend/dist"})  # and any __pycache__
# The committed files that ship, and the paths whose index entries must be plain "H".
PAYLOAD_GIT_PATHS = ("backend/app", "backend/run_packaged.py")
INDEX_CHECK_PATHS = ("backend/app", "backend/run_packaged.py", "backend/requirements.txt", "frontend")
FRONTEND_ENV_RE = re.compile(r"^\.env", re.IGNORECASE)  # .env, .env.local, .env.production, ...
VERSION_FILE = "backend/app/version.py"
NPM_CI = ["npm", "ci", "--ignore-scripts"]  # no package install scripts run on the release PC
EMAIL_TEXT = (
    "Here's an Iron Owl update. Save the attached file, then open Iron Owl; "
    "it will offer to install it."
)
# Optional personal greeting for EMAIL.txt, kept out of git (.gitignore): plain UTF-8 text.
EMAIL_GREETING_REL = Path("tools") / "release" / "email_greeting.local.txt"
EMAIL_GREETING_MAX_CHARS = 1000
RELEASES_REL = Path("tools") / "release" / "releases.json"
NOTES_NAME = "RELEASE_NOTES.md"
# The update file (and GitHub release asset) name; the app finds files by the extension.
PACKAGE_NAME = "Iron-Owl-{version}.ftupdate"
# The unsigned GitHub build (--unsigned-out / --sign-only): exactly these two files.
UNSIGNED_MANIFEST = "manifest.json"
UNSIGNED_PAYLOAD = "payload.bin"
SOURCES = ("file", "github")
# Which update_keys.py dict signs and verifies each source, and the key ids it takes.
KEYS_FOR_SOURCE = {"file": "TRUSTED_KEYS", "github": "GITHUB_KEYS"}
KEY_ID_FOR_SOURCE = {"file": re.compile(r"^ft-[0-9]{4}[a-z]$"), "github": re.compile(r"^io-[0-9]{4}[a-z]$")}
# GitHub "owner/name", the same rule as the app (backend/app/updates/source.py valid_repo).
_REPO_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_PASSPHRASE_STDIN = 1024
BANNED_NOTE_WORDS = re.compile(
    r"\b(servers?|ports?|checksums?|paths?|sha(?:-?256)?|hash(?:es)?|localhost|https?|urls?|api|"
    r"backend|frontend|python|exceptions?|traceback|stack trace|error codes?|ft-[a-z]+-[0-9a-z]+|"
    r"127\.0\.0\.1|json|sqlite|database migration)\b",
    re.IGNORECASE,
)
# Never in a payload even if someone adds them under a shipped folder.
_DENIED = re.compile(
    r"(^|/)(\.env[^/]*|data|design|\.claude|\.git[^/]*|node_modules|keyfile\.json[^/]*|[^/]*\.db"
    r"|[^/]*\.pem|[^/]*\.ftbackup)(/|$)",
    re.IGNORECASE,
)


class BuildError(Exception):
    pass


Git = Callable[[list[str]], str]
BlobReader = Callable[[list[str]], dict[str, bytes]]  # blob ids -> exact committed bytes
Runner = Callable[[list[str], Path], None]


def default_git(repo_root: Path) -> Git:
    def git(args: list[str]) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo_root), *args], capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuildError(f"git {' '.join(args)} failed: {exc}") from None
        return done.stdout
    return git


def default_blob_reader(repo_root: Path) -> BlobReader:
    """``git cat-file --batch``: the committed bytes of each blob, exactly (no text mode,
    no attributes or filters applied)."""
    def read(shas: list[str]) -> dict[str, bytes]:
        if not shas:
            return {}
        if any(not re.fullmatch(r"[0-9a-f]{40,64}", sha) for sha in shas):
            raise BuildError("git reported an unexpected object id.")
        try:
            done = subprocess.run(
                ["git", "-C", str(repo_root), "cat-file", "--batch"],
                input="".join(f"{sha}\n" for sha in shas).encode("ascii"),
                capture_output=True, check=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuildError(f"git cat-file failed: {exc}") from None
        return parse_cat_file_batch(done.stdout, shas)
    return read


def parse_cat_file_batch(out: bytes, shas: list[str]) -> dict[str, bytes]:
    """``<id> blob <size>`` LF ``<bytes>`` LF, once per requested id, in order."""
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        end = out.find(b"\n", pos)
        if end < 0:
            raise BuildError("git cat-file output was cut short.")
        header = out[pos:end].decode("ascii", "replace").split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob" or not header[2].isdigit():
            raise BuildError(f"git cat-file could not read {sha}.")
        start, size = end + 1, int(header[2])
        if out[start + size:start + size + 1] != b"\n":
            raise BuildError("git cat-file output was cut short.")
        blobs[sha] = out[start:start + size]
        pos = start + size + 1
    if pos != len(out):
        raise BuildError("git cat-file printed more than was asked for.")
    return blobs


def default_runner(cmd: list[str], cwd: Path) -> None:
    print(f"$ {' '.join(cmd)}  (in {cwd})", flush=True)
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        subprocess.run([exe, *cmd[1:]], cwd=cwd, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"{' '.join(cmd)} failed: {exc}") from None


@dataclass
class BuildConfig:
    repo_root: Path
    version: str
    kind: str = "app"
    out_dir: Path | None = None
    key_path: Path = field(default_factory=keygen.default_key_path)
    min_current_version: str = "1.0.0"
    requires_reinstall: bool = False
    python: str = "3.11"
    bootstrap_version: int = 1
    released_at: str | None = None  # "YYYY-MM-DDTHH:MM:SSZ"; now when None
    branch: str = "master"
    commit: bool = False
    source: str = "file"  # "file" (emailed) or "github" (public releases)
    update_repo: str | None = None  # "owner/name", github only
    tag: str | None = None  # github only: HEAD must be this tag ("vX.Y.Z")
    unsigned_out: Path | None = None  # github only: write the unsigned manifest + payload here

    @property
    def output_dir(self) -> Path:
        return self.out_dir or (self.repo_root / "dist-release")

    @property
    def github(self) -> bool:
        return self.source == "github"


@dataclass
class BuildResult:
    package: Path
    sha256: str
    size: int
    manifest: dict
    payload_names: list[str]
    warnings: list[str]


# ------------------------------------------------------------------ checks


def valid_repo(value: object) -> bool:
    """GitHub "owner/name": owner 1-39 of [A-Za-z0-9-] without a leading, trailing or double
    hyphen; name 1-100 of [A-Za-z0-9._-], not "." or ".." and not ending in ".git"."""
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    return bool(
        _REPO_OWNER.fullmatch(owner) and _REPO_NAME.fullmatch(name)
        and name not in (".", "..") and not name.lower().endswith(".git")
    )


def check_source(cfg: BuildConfig) -> None:
    if cfg.source not in SOURCES:
        raise BuildError("--source must be file or github.")
    if not cfg.github:
        if cfg.update_repo or cfg.tag:
            raise BuildError("--update-repo and --tag are only for --source github.")
        return
    if not valid_repo(cfg.update_repo):
        raise BuildError("--source github needs --update-repo <owner>/<name> (a GitHub repository name).")
    if cfg.tag != f"v{cfg.version}":
        raise BuildError(f"--source github needs --tag v{cfg.version} (the tag being released).")
    if cfg.commit:
        raise BuildError("--commit is for emailed builds; a GitHub build is made from a tag.")
    if cfg.unsigned_out is not None and cfg.unsigned_out.exists() and (
        not cfg.unsigned_out.is_dir() or any(cfg.unsigned_out.iterdir())
    ):
        raise BuildError("--unsigned-out must be a new or empty folder.")


def check_head_is_tag(git: Git, tag: str) -> None:
    """GitHub builds: HEAD must be exactly the commit the release tag points at."""
    head = git(["rev-parse", "--verify", "HEAD^{commit}"]).strip()
    tagged = git(["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"]).strip()
    if not head or head != tagged:
        raise BuildError(f"HEAD is not the commit tagged {tag}.")


def read_email_text(repo_root: Path) -> str:
    """The body of EMAIL.txt: the local greeting file's text if present, else EMAIL_TEXT.

    The file is read as text only: at most EMAIL_GREETING_MAX_CHARS characters of UTF-8, no
    control or invisible format characters other than line breaks. Anything else stops the
    build (the message names the file, never its contents or full path)."""
    name = EMAIL_GREETING_REL.name
    path = repo_root / EMAIL_GREETING_REL
    if not path.exists():
        return EMAIL_TEXT
    if not path.is_file():
        raise BuildError(f"{name} is not a plain file.")
    try:
        with path.open("rb") as fh:
            raw = fh.read(EMAIL_GREETING_MAX_CHARS * 4 + 4)  # UTF-8: at most 4 bytes a character, + BOM
            too_big = bool(fh.read(1))
    except OSError:
        raise BuildError(f"{name} can't be read.") from None
    if too_big:
        raise BuildError(f"{name} is too long (at most {EMAIL_GREETING_MAX_CHARS} characters).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BuildError(f"{name} must be saved as UTF-8 text.") from None
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return EMAIL_TEXT
    if len(text) > EMAIL_GREETING_MAX_CHARS:
        raise BuildError(f"{name} is too long (at most {EMAIL_GREETING_MAX_CHARS} characters).")
    if any(ch != "\n" and unicodedata.category(ch) in _ODD_CATEGORIES for ch in text):
        raise BuildError(f"{name} has odd characters (only text and line breaks are allowed).")
    return text


# control, format (bidi and zero-width), surrogate, private-use, unassigned, line/paragraph separators
_ODD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _status_path(line: str) -> str:
    return line[3:].strip()


def _allowed_ignored(line: str) -> bool:
    path = _status_path(line)
    return line.startswith("!! ") and ("__pycache__" in path.split("/") or path.rstrip("/") in ALLOWED_IGNORED)


def check_clean_tree(git: Git, branch: str | None, *, allow_version_bump: bool = False) -> None:
    """Refuse to build from anything but a clean checkout of ``branch`` (None: a GitHub build
    from a tag, checked by ``check_head_is_tag`` instead).

    Also refuses ignored or untracked files under the paths the build reads from
    (``SOURCE_DIRS``), except ``__pycache__``, ``frontend/node_modules`` and
    ``frontend/dist``, and index entries that hide edits from ``git status``. After the
    build steps (``allow_version_bump``) the only change allowed is the version.py bump."""
    if branch is not None:
        head = git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if head != branch:
            raise BuildError(f"Releases are built from {branch}; this checkout is on {head or '?'}.")
    def version_bump(line: str) -> bool:
        return allow_version_bump and line[:2] in (" M", "M ", "MM") and _status_path(line) == VERSION_FILE

    dirty = [
        line for line in git(["status", "--porcelain", "--untracked-files=normal"]).splitlines()
        if line.strip() and not version_bump(line)
    ]
    if dirty:
        when = "after the build steps" if allow_version_bump else "before building"
        raise BuildError(f"The working tree has uncommitted changes ({when}):\n" + "\n".join(dirty))
    extra = [
        line for line in git(["status", "--porcelain", "--ignored", "--", *SOURCE_DIRS]).splitlines()
        # (this listing shows tracked changes under SOURCE_DIRS too: the bump is one of them)
        if line.strip() and not _allowed_ignored(line) and not version_bump(line)
    ]
    if extra:
        raise BuildError(
            "Ignored or untracked files where the build reads source (move or delete them):\n" + "\n".join(extra)
        )
    check_index_flags(git)


def check_index_flags(git: Git) -> None:
    """Every index entry under the payload sources must be tagged ``H`` by ``git ls-files
    -v``: skip-worktree (``S``), assume-unchanged (lowercase) and the like make ``git
    status`` blind to edits, so a "clean" tree could still feed changed files to the build."""
    out = git(["ls-files", "-v", "-z", "--", *INDEX_CHECK_PATHS])
    odd = [entry for entry in out.split("\0") if entry and not entry.startswith("H ")]
    if odd:
        raise BuildError(
            "Files marked skip-worktree or assume-unchanged (clear it with git update-index "
            "--no-skip-worktree --no-assume-unchanged <file>):\n" + "\n".join(odd)
        )


def check_frontend_env_files(repo_root: Path) -> None:
    """Refuse any env-style file under frontend (outside node_modules): Vite reads them and
    bakes their values into the web build whether or not git tracks them."""
    root = repo_root / "frontend"
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if not rel.parts:
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
        found += [(rel / name).as_posix() for name in (*dirnames, *filenames) if FRONTEND_ENV_RE.match(name)]
    if found:
        raise BuildError("Env files under frontend (Vite would bake them into the build):\n"
                         + "\n".join(f"frontend/{name}" for name in sorted(found)))


def committed_files(git: Git, blobs: BlobReader) -> dict[str, bytes]:
    """backend/app/** and backend/run_packaged.py exactly as committed at HEAD (from the
    object store, never the working tree): the only Python that ships."""
    out = git(["ls-tree", "-r", "-z", "HEAD", "--", *PAYLOAD_GIT_PATHS])
    ids: dict[str, str] = {}
    for record in out.split("\0"):
        if not record:
            continue
        meta, tab, path = record.partition("\t")
        fields = meta.split()
        if not tab or not path or len(fields) != 3:
            raise BuildError(f"Unexpected git ls-tree output: {record!r}")
        mode, kind, sha = fields
        if kind != "blob" or mode not in ("100644", "100755"):
            raise BuildError(f"{path} is committed as a link or submodule ({mode} {kind}); refusing to ship it.")
        ids[path] = sha
    contents = blobs(sorted(set(ids.values())))
    try:
        return {path: contents[sha] for path, sha in sorted(ids.items())}
    except KeyError:
        raise BuildError("git did not return every committed file.") from None


def read_release_notes(repo_root: Path, version: str) -> tuple[list[str], list[str]]:
    """The bullets under ``## <version>``; returns (notes, warnings). Raises BuildError."""
    path = repo_root / NOTES_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise BuildError(f"{NOTES_NAME} is missing or unreadable.") from None
    start = next((i for i, line in enumerate(lines) if line.strip() == f"## {version}"), None)
    if start is None:
        raise BuildError(f"{NOTES_NAME} has no '## {version}' section.")
    notes: list[str] = []
    problems: list[str] = []
    for line in lines[start + 1:]:
        text = line.strip()
        if text.startswith("#"):
            break  # the next section (or any other heading)
        if not text:
            continue
        if not text.startswith(("- ", "* ")):
            problems.append(f"not a bullet point: {text!r}")
            continue
        note = text[2:].strip()
        if not pkg._valid_note(note):  # noqa: SLF001 - the exact rule the app enforces
            problems.append(f"empty, over {pkg.MAX_NOTE_CHARS} characters or has odd characters: {note!r}")
        elif match := BANNED_NOTE_WORDS.search(note):
            problems.append(f"uses a word the user shouldn't see ({match.group(0)!r}): {note!r}")
        notes.append(note)
    if not 1 <= len(notes) <= pkg.MAX_NOTES:
        problems.append(f"needs 1 to {pkg.MAX_NOTES} bullet points (2 to 4 is best); found {len(notes)}")
    if problems:
        raise BuildError(f"{NOTES_NAME} section {version}:\n  " + "\n  ".join(problems))
    warnings = [] if 2 <= len(notes) <= 4 else [f"{len(notes)} release notes; 2 to 4 reads best"]
    return notes, warnings


# ``(?=\r?$)``: a CRLF checkout (core.autocrlf) matches too and keeps its line ending when bumped.
_VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"[ \t]*(?=\r?$)', re.MULTILINE)


def read_app_version(repo_root: Path) -> str:
    text = (repo_root / "backend" / "app" / "version.py").read_text(encoding="utf-8")
    m = _VERSION_LINE.search(text)
    if not m:
        raise BuildError("backend/app/version.py has no __version__ line.")
    return m.group(1)


def bump_version_text(text: str, version: str) -> str:
    if not _VERSION_LINE.search(text):
        raise BuildError("backend/app/version.py has no __version__ line.")
    return _VERSION_LINE.sub(f'__version__ = "{version}"', text, count=1)


def write_app_version(repo_root: Path, version: str) -> str:
    """Set __version__; returns the file's previous text (to restore on failure)."""
    path = repo_root / "backend" / "app" / "version.py"
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    path.write_bytes(bump_version_text(text, version).encode("utf-8"))
    return text


def read_schema_version(repo_root: Path) -> int:
    text = (repo_root / "backend" / "app" / "migrations.py").read_text(encoding="utf-8")
    m = re.search(r"^LATEST[ \t]*=[ \t]*([0-9]+)[ \t]*$", text, re.MULTILINE)
    if not m:
        raise BuildError("backend/app/migrations.py has no LATEST = N line.")
    return int(m.group(1))


def read_trusted_keys(repo_root: Path, name: str = "TRUSTED_KEYS") -> dict[str, str]:
    """A key dict (TRUSTED_KEYS, or GITHUB_KEYS for GitHub builds) from a checkout's
    update_keys.py, parsed (never imported)."""
    source = (repo_root / "backend" / "app" / "update_keys.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
        )
        if isinstance(target, ast.Name) and target.id == name and node.value is not None:
            value = ast.literal_eval(node.value)
            if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                return dict(value)
    raise BuildError(f"backend/app/update_keys.py has no {name} dict.")


def keys_for(repo_root: Path, source: str) -> dict[str, str]:
    """The keys that sign and verify ``source``'s updates. A GitHub build never uses the
    emailed-update key and vice versa (the key id prefix must match the source)."""
    name = KEYS_FOR_SOURCE[source]
    keys = read_trusted_keys(repo_root, name)
    if not keys:
        raise BuildError(f"backend/app/update_keys.py {name} is empty: add the public signing key first.")
    pattern = KEY_ID_FOR_SOURCE[source]
    odd = sorted(k for k in keys if not pattern.fullmatch(k))
    if odd:
        raise BuildError(f"{name} has key ids that don't belong to {source} builds: {', '.join(odd)}")
    return keys


def load_releases(repo_root: Path) -> list[dict]:
    path = repo_root / RELEASES_REL
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BuildError(f"{RELEASES_REL} is unreadable.") from None
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, list):
        raise BuildError(f"{RELEASES_REL} has no releases list.")
    return releases


normalized_requirements = deps_id_tool.normalized_requirements


def compute_deps_id(repo_root: Path) -> str:
    """The runtime id of backend/requirements.txt: same deps_id = same site-packages. The
    exact id tools/release/deps_id.py gives build_package.ps1, so the runtimes/<deps_id>
    folder the installer made is the one an ``app`` update looks for."""
    text = (repo_root / "backend" / "requirements.txt").read_text(encoding="utf-8")
    try:
        return deps_id_tool.deps_id(text)
    except ValueError as exc:
        raise BuildError(f"backend/requirements.txt: {exc}") from None


# ------------------------------------------------------------------ payload


def _add(files: dict[str, Path | bytes], name: str, source: Path | bytes, kind: str) -> None:
    if isinstance(source, Path) and source.is_symlink():
        raise BuildError(f"{name} is a link; refusing to ship it.")
    if len(name) > PAYLOAD_PATH_BUDGET:
        raise BuildError(f"{name} is longer than {PAYLOAD_PATH_BUDGET} characters; shorten the name or folder.")
    if not name.startswith("site-packages/") and _DENIED.search(name):
        raise BuildError(f"{name} must never be shipped.")
    problem = pkg.path_problem(name)
    if problem:
        raise BuildError(f"{name}: {problem}")
    if not pkg.whitelisted(name, kind):
        raise BuildError(f"{name} is not allowed in an update payload (see the whitelist).")
    folded = pkg._fold(name)  # noqa: SLF001
    if any(pkg._fold(existing) == folded for existing in files):  # noqa: SLF001
        raise BuildError(f"{name} differs from another file only by case.")
    files[name] = source


def _walk(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if "__pycache__" not in p.parts)


def collect_payload(
    repo_root: Path, kind: str, site_packages: Path | None = None, *, committed: dict[str, bytes],
) -> dict[str, Path | bytes]:
    """name -> source (committed bytes, or a build-output file). backend/app and the
    launcher come from ``committed`` (``committed_files``), never the working tree; only the
    other whitelisted roots (build outputs) are walked."""
    files: dict[str, Path | bytes] = {}
    for rel, data in committed.items():
        parts = rel.split("/")
        if parts[:2] != ["backend", "app"] or len(parts) < 3 or "__pycache__" in parts:
            continue
        suffix = Path(parts[-1]).suffix
        if suffix in (".pyc", ".pyo") or suffix.lower() == ".md":
            continue  # bytecode is rebuilt on install; READMEs are for developers
        _add(files, rel, data, kind)
    launcher = committed.get("backend/run_packaged.py")
    if launcher is None:
        raise BuildError("backend/run_packaged.py is missing or not committed (it comes with the launcher work).")
    _add(files, "run_packaged.py", launcher, kind)
    dist = repo_root / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise BuildError("frontend/dist/index.html is missing; the web build didn't run.")
    for path in _walk(dist):
        if path.is_dir() and not path.is_symlink():
            continue
        _add(files, "web/" + path.relative_to(dist).as_posix(), path, kind)
    if kind == "full":
        if site_packages is None or not site_packages.is_dir():
            raise BuildError("A full update needs the site-packages folder.")
        for path in _walk(site_packages):
            if path.is_dir() and not path.is_symlink():
                continue
            if path.suffix in (".pyc", ".pyo"):
                continue
            _add(files, "site-packages/" + path.relative_to(site_packages).as_posix(), path, kind)
    return files


def release_json(version: str, schema_version: int, deps_id: str, kind: str, python: str, bootstrap: int,
                 *, update_repo: str | None = None) -> bytes:
    """The payload's release.json. With ``update_repo`` (GitHub builds only) it also says
    where updates come from; emailed builds never carry those keys (installed versions
    before 2.0.0 refuse any key they don't know)."""
    data: dict = {"version": version, "schema_version": schema_version, "deps_id": deps_id,
                  "kind": kind, "python": python, "bootstrap_version": bootstrap}
    if update_repo is not None:
        data.update({"update_source": "github", "update_repo": update_repo})
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _zip_info(name: str, compress: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = FILE_MODE << 16
    info.compress_type = compress
    return info


def deterministic_zip(entries: list[tuple[str, bytes]], *, compress: int, sort: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in (sorted(entries) if sort else entries):
            zf.writestr(_zip_info(name, compress), data, compress_type=compress, compresslevel=9)
    return buffer.getvalue()


def build_payload(files: dict[str, Path | bytes], release: bytes) -> tuple[bytes, int, int]:
    """(payload.zip bytes, file count, unpacked size)."""
    entries = [(pkg.RELEASE_NAME, release)]
    entries += [(name, src if isinstance(src, bytes) else src.read_bytes()) for name, src in files.items()]
    unpacked = sum(len(data) for _, data in entries)
    return deterministic_zip(entries, compress=zipfile.ZIP_DEFLATED), len(entries), unpacked


canonical_json = pkg.canonical_json


def sign_manifest(manifest_bytes: bytes, private_key, trusted_keys: dict[str, str],
                  keys_name: str = "TRUSTED_KEYS") -> tuple[bytes, str]:
    public = keygen.public_key_b64(private_key)
    key_id = next((k for k, v in trusted_keys.items() if v == public), None)
    if key_id is None:
        raise BuildError(f"This signing key's public key is not in backend/app/update_keys.py {keys_name}.")
    signature = private_key.sign(pkg.SIGNATURE_DOMAIN + manifest_bytes)
    sig = {"alg": pkg.SIG_ALG, "key_id": key_id, "sig": base64.b64encode(signature).decode("ascii")}
    return canonical_json(sig), key_id


def package_file(manifest_bytes: bytes, sig_bytes: bytes, encoded_payload: bytes) -> bytes:
    """The v2 container (not a zip): magic + length-prefixed manifest, signature, payload."""
    return pkg.pack_container(manifest_bytes, sig_bytes, encoded_payload)


# ------------------------------------------------------------------ the build


def _now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    cfg: BuildConfig,
    *,
    git: Git | None = None,
    blobs: BlobReader | None = None,
    runner: Runner = default_runner,
    passphrase: Callable[[], str] | None = None,
    python_exe: str = sys.executable,
) -> BuildResult:
    repo = cfg.repo_root
    git = git or default_git(repo)
    blobs = blobs or default_blob_reader(repo)
    passphrase = passphrase or (lambda: getpass.getpass("Signing key passphrase: "))
    warnings: list[str] = []

    if cfg.kind not in ("app", "full"):
        raise BuildError("--kind must be app or full.")
    for label, value in (("version", cfg.version), ("min current version", cfg.min_current_version)):
        if not semver.is_valid(value):
            raise BuildError(f"The {label} must look like 1.4.0.")
    if semver.compare(cfg.min_current_version, cfg.version) >= 0:
        raise BuildError("The minimum current version must be older than the new version.")
    check_source(cfg)
    if cfg.unsigned_out is not None and not cfg.github:
        raise BuildError("--unsigned-out is only for --source github.")
    if cfg.github and cfg.unsigned_out is None:
        raise BuildError("A GitHub build has two steps: --unsigned-out <folder> (no key), then --sign-only <folder>.")

    if cfg.github:
        check_clean_tree(git, None)
        check_head_is_tag(git, cfg.tag or "")
    else:
        check_clean_tree(git, cfg.branch)
    check_frontend_env_files(repo)
    notes, note_warnings = read_release_notes(repo, cfg.version)
    warnings += note_warnings
    email_text = None if cfg.github else read_email_text(repo)

    # releases.json is the emailed builds' history (it never ships publicly): GitHub builds
    # neither read nor write it.
    releases = [] if cfg.github else load_releases(repo)
    previous = releases[-1] if releases else None
    current = read_app_version(repo)
    if cfg.github and current != cfg.version:
        raise BuildError(f"The tag says {cfg.version} but version.py says {current}: they must match.")
    if previous and semver.compare(cfg.version, previous["version"]) <= 0:
        raise BuildError(f"{cfg.version} is not newer than the last release {previous['version']}.")
    if semver.is_valid(current) and semver.compare(cfg.version, current) < 0:
        raise BuildError(f"{cfg.version} is older than version.py ({current}).")
    deps_id = compute_deps_id(repo)
    if cfg.kind == "app" and previous and previous.get("deps_id") != deps_id:
        raise BuildError(
            f"The runtime requirements changed since {previous['version']}: build this release with --kind full."
        )
    keys_name = KEYS_FOR_SOURCE[cfg.source]
    trusted = keys_for(repo, cfg.source) if cfg.github else read_trusted_keys(repo)
    previous_keys = previous.get("trusted_keys") if previous else trusted
    if not isinstance(previous_keys, dict) or not previous_keys:
        raise BuildError("No trusted keys to self-verify with (run keygen and fill update_keys.py).")

    original_version_text = write_app_version(repo, cfg.version) if current != cfg.version else None
    tmp = Path(tempfile.mkdtemp(prefix="ftrelease-"))
    try:
        # Own --basetemp: the shared default (Temp\pytest-of-<user>) can be left unreadable.
        runner([python_exe, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp / "pytest")],
               repo / "backend")
        runner(NPM_CI, repo / "frontend")
        runner(["npm", "run", "build"], repo / "frontend")
        site_packages = None
        if cfg.kind == "full":
            site_packages = tmp / "site-packages"
            # The hash lock when there is one, installed as build_package.ps1 does: exact,
            # hash-checked wheels, plus the allowlisted sdists (plaid-python) built into wheels
            # with the hash-pinned build backend (make_lock.py install-runtime).
            lock = repo / "tools" / "release" / "requirements-lock.txt"
            if lock.is_file():
                runner([python_exe, str(HERE / "make_lock.py"), "install-runtime", str(lock), str(site_packages),
                        "--python-version", cfg.python], repo / "backend")
            else:
                runner([
                    python_exe, "-m", "pip", "install", "--no-compile", "--only-binary=:all:",
                    "--platform", "win_amd64", "--python-version", cfg.python, "--implementation", "cp",
                    "--target", str(site_packages), "-r", str(repo / "backend" / "requirements.txt"),
                ], repo / "backend")

        # the build steps must not have changed or added anything that feeds the payload
        check_clean_tree(git, None if cfg.github else cfg.branch, allow_version_bump=not cfg.github)
        check_frontend_env_files(repo)
        committed = committed_files(git, blobs)
        if VERSION_FILE not in committed:
            raise BuildError(f"{VERSION_FILE} is not committed.")
        try:  # the one change from HEAD that ships: the version bump
            bumped = bump_version_text(committed[VERSION_FILE].decode("utf-8"), cfg.version)
        except UnicodeDecodeError:
            raise BuildError(f"{VERSION_FILE} is not UTF-8.") from None
        committed[VERSION_FILE] = bumped.encode("utf-8")
        files = collect_payload(repo, cfg.kind, site_packages, committed=committed)
        schema_version = read_schema_version(repo)
        release = release_json(cfg.version, schema_version, deps_id, cfg.kind, cfg.python, cfg.bootstrap_version,
                               update_repo=cfg.update_repo if cfg.github else None)
        payload, count, unpacked = build_payload(files, release)
        if unpacked > pkg.UNPACKED_CAPS[cfg.kind] or count > pkg.MAX_PAYLOAD_ENTRIES:
            raise BuildError("The payload is larger than an installed Iron Owl accepts.")
        encoded = pkg.encode_payload(payload)
        manifest = {
            "schema": pkg.MANIFEST_SCHEMA, "app": "fintrack", "version": cfg.version,
            "released_at": cfg.released_at or _now_stamp(),
            "min_current_version": cfg.min_current_version, "requires_reinstall": cfg.requires_reinstall,
            "kind": cfg.kind, "python": cfg.python, "bootstrap_version": cfg.bootstrap_version,
            "schema_version": schema_version, "deps_id": deps_id, "notes": notes,
            "payload": {"size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
                        "files": count, "unpacked_size": unpacked},
        }
        manifest_bytes = canonical_json(manifest)
        try:
            pkg._parse_manifest(manifest_bytes)  # noqa: SLF001 - the app's own strict parser
        except pkg.UpdateRejected:
            raise BuildError("The manifest doesn't pass the app's own checks.") from None
        payload_names = sorted([pkg.RELEASE_NAME, *files])
        if cfg.unsigned_out is not None:  # GitHub step 1: no key, no passphrase
            return write_unsigned(cfg.unsigned_out, manifest, manifest_bytes, encoded, payload_names, warnings)

        final, digest, size, key_id = _sign_and_write(
            cfg, manifest_bytes, encoded, trusted, keys_name, previous, previous_keys, tmp, passphrase, warnings,
        )
        out = cfg.output_dir
        (out / "EMAIL.txt").write_text(
            f"Subject: Iron Owl update {semver.display(cfg.version)}\n\n{email_text}\n", encoding="utf-8",
        )
        releases.append({
            "version": cfg.version, "kind": cfg.kind, "deps_id": deps_id, "schema_version": schema_version,
            "released_at": manifest["released_at"], "file": final.name, "sha256": digest, "size": size,
            "key_id": key_id, "trusted_keys": trusted,
        })
        releases_path = repo / RELEASES_REL
        releases_path.parent.mkdir(parents=True, exist_ok=True)
        releases_path.write_text(json.dumps({"releases": releases}, indent=2) + "\n", encoding="utf-8")
        original_version_text = None  # success: keep the bump
        if cfg.commit:
            git(["add", "backend/app/version.py", RELEASES_REL.as_posix()])
            git(["commit", "-m", f"Release {cfg.version}"])
            git(["tag", "-a", f"v{cfg.version}", "-m", f"Iron Owl {cfg.version}"])
        return BuildResult(final, digest, size, manifest, payload_names, warnings)
    finally:
        if original_version_text is not None:
            (repo / "backend" / "app" / "version.py").write_bytes(original_version_text.encode("utf-8"))
        shutil.rmtree(tmp, ignore_errors=True)


def _sign_and_write(
    cfg: BuildConfig, manifest_bytes: bytes, encoded: bytes, trusted: dict[str, str], keys_name: str,
    previous: dict | None, previous_keys: dict, tmp: Path, passphrase: Callable[[], str], warnings: list[str],
) -> tuple[Path, str, int, str]:
    """Sign the manifest, pack the container, self-verify, check sizes and write the
    .ftupdate and its .sha256 into the output folder. (final path, sha256, size, key id)"""
    try:
        private_key = keygen.load_private_key(cfg.key_path, passphrase())
    except FileNotFoundError:
        raise BuildError(f"No signing key at {cfg.key_path} (run keygen.py once).") from None
    except (ValueError, TypeError) as exc:
        raise BuildError(f"Can't use the signing key: {exc or 'wrong passphrase'}") from None
    sig_bytes, key_id = sign_manifest(manifest_bytes, private_key, trusted, keys_name)
    del private_key
    package_bytes = package_file(manifest_bytes, sig_bytes, encoded)

    name = PACKAGE_NAME.format(version=cfg.version)
    staged = tmp / name
    staged.write_bytes(package_bytes)
    warnings += _self_verify(staged, cfg, previous, previous_keys, tmp)

    size = len(package_bytes)
    if size >= pkg.MAX_PACKAGE_BYTES:
        raise BuildError("The package is larger than an installed Iron Owl accepts.")
    if cfg.kind == "app" and size > APP_SIZE_LIMIT:
        raise BuildError(f"An app update must be under 10 MB to email; this one is {size / MB:.1f} MB.")
    if cfg.kind == "full" and size > FULL_SIZE_WARNING:
        warnings.append(f"{size / MB:.1f} MB is too big for most email: use a Drive link or USB.")

    digest = hashlib.sha256(package_bytes).hexdigest()
    out = cfg.output_dir
    out.mkdir(parents=True, exist_ok=True)
    final = out / name
    # Temp is usually on C: and the repo may be on D:; os.replace can't cross drives, so
    # write a partial file next to the target and rename it there (atomic on one drive).
    partial = out / f"{name}.partial"
    partial.write_bytes(package_bytes)
    os.replace(partial, final)
    (out / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")
    return final, digest, size, key_id


# ------------------------------------------------------------------ GitHub: two steps


def write_unsigned(folder: Path, manifest: dict, manifest_bytes: bytes, encoded: bytes,
                   payload_names: list[str], warnings: list[str]) -> BuildResult:
    """GitHub step 1: the manifest (exact bytes) and the encoded payload, for --sign-only."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / UNSIGNED_MANIFEST).write_bytes(manifest_bytes)
    (folder / UNSIGNED_PAYLOAD).write_bytes(encoded)
    return BuildResult(folder, hashlib.sha256(manifest_bytes).hexdigest(), len(encoded), manifest,
                       payload_names, warnings)


def _read_bundle_file(folder: Path, name: str, limit: int) -> bytes:
    path = folder / name
    if path.is_symlink() or not path.is_file():
        raise BuildError(f"The unsigned folder has no {name}.")
    with path.open("rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise BuildError(f"The unsigned {name} is too large.")
    return data


def read_unsigned(folder: Path, cfg: BuildConfig) -> tuple[dict, bytes, bytes, list[str]]:
    """Check what step 1 made before anything is signed: exactly two files, a manifest the
    app's own parser accepts (canonical bytes, this version), a payload whose size and
    SHA-256 match it, and a release.json for this version, source and repository.
    (manifest, manifest bytes, encoded payload, payload names)"""
    if folder.is_symlink() or not folder.is_dir():
        raise BuildError("--sign-only needs the folder the build step made (--unsigned-out).")
    names = sorted(p.name for p in folder.iterdir())
    if names != sorted([UNSIGNED_MANIFEST, UNSIGNED_PAYLOAD]):
        raise BuildError(f"The unsigned folder must hold only {UNSIGNED_MANIFEST} and {UNSIGNED_PAYLOAD}.")
    manifest_bytes = _read_bundle_file(folder, UNSIGNED_MANIFEST, pkg.MAX_MANIFEST_BYTES)
    encoded = _read_bundle_file(folder, UNSIGNED_PAYLOAD, pkg.MAX_PAYLOAD_BYTES)
    try:
        pkg._parse_manifest(manifest_bytes)  # noqa: SLF001 - the app's own strict parser
        manifest = json.loads(manifest_bytes)
    except (pkg.UpdateRejected, ValueError):
        raise BuildError("The unsigned manifest doesn't pass the app's own checks.") from None
    if canonical_json(manifest) != manifest_bytes:
        raise BuildError("The unsigned manifest is not in canonical form.")
    if manifest["version"] != cfg.version:
        raise BuildError(f"The unsigned manifest is for {manifest['version']}, not {cfg.version}.")
    info = manifest["payload"]
    if info["size"] != len(encoded) or info["sha256"] != hashlib.sha256(encoded).hexdigest():
        raise BuildError("The unsigned payload doesn't match its manifest.")
    try:
        with zipfile.ZipFile(io.BytesIO(pkg.decode_payload(encoded))) as zf:
            payload_names = sorted(zf.namelist())
            if zf.getinfo(pkg.RELEASE_NAME).file_size > pkg.MAX_RELEASE_JSON_BYTES:
                raise ValueError("release.json too large")
            release = json.loads(zf.read(pkg.RELEASE_NAME))
    except Exception:  # noqa: BLE001 - a bad zip can fail in many ways; none may be signed
        raise BuildError("The unsigned payload has no readable release.json.") from None
    if not isinstance(release, dict) or (release.get("version"), release.get("update_source"),
                                         release.get("update_repo")) != (cfg.version, "github", cfg.update_repo):
        raise BuildError("The unsigned payload's release.json is not for this version and repository.")
    return manifest, manifest_bytes, encoded, payload_names


def sign_only(
    cfg: BuildConfig,
    folder: Path,
    *,
    git: Git | None = None,
    passphrase: Callable[[], str] | None = None,
) -> BuildResult:
    """GitHub step 2: sign what step 1 made, from a clean checkout of the tag. Builds
    nothing and runs no program but git (no tests, npm or pip)."""
    repo = cfg.repo_root
    git = git or default_git(repo)
    passphrase = passphrase or (lambda: getpass.getpass("Signing key passphrase: "))
    if not cfg.github:
        raise BuildError("--sign-only is only for --source github.")
    if cfg.unsigned_out is not None:
        raise BuildError("--sign-only and --unsigned-out are separate steps.")
    if not semver.is_valid(cfg.version):
        raise BuildError("The version must look like 1.4.0.")
    check_source(cfg)
    check_clean_tree(git, None)
    check_head_is_tag(git, cfg.tag or "")
    current = read_app_version(repo)
    if current != cfg.version:
        raise BuildError(f"The tag says {cfg.version} but version.py says {current}: they must match.")
    trusted = keys_for(repo, cfg.source)
    manifest, manifest_bytes, encoded, payload_names = read_unsigned(folder, cfg)
    # The self-check and the size rules use what the manifest says.
    cfg = replace(cfg, kind=manifest["kind"], min_current_version=manifest["min_current_version"],
                  requires_reinstall=manifest["requires_reinstall"], python=manifest["python"],
                  bootstrap_version=manifest["bootstrap_version"])
    warnings: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="ftsign-"))
    try:
        final, digest, size, _ = _sign_and_write(
            cfg, manifest_bytes, encoded, trusted, KEYS_FOR_SOURCE[cfg.source], None, trusted, tmp, passphrase,
            warnings,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return BuildResult(final, digest, size, manifest, payload_names, warnings)


def _self_verify(path: Path, cfg: BuildConfig, previous: dict | None, previous_keys: dict, tmp: Path) -> list[str]:
    """Verify exactly as the installed copy will (its keys), then a trial extraction.
    Returns warnings."""
    floor = previous["version"] if previous else "0.0.0"
    current = max(floor, cfg.min_current_version, key=semver.parse)
    try:
        verified = pkg.verify_package(path, current, trusted_keys=previous_keys)
    except pkg.UpdateRejected as exc:
        raise BuildError(f"Self-check failed: {exc.code}") from None
    if verified.result != "ready":
        raise BuildError(f"Self-check: the package is not newer than {current}.")
    if cfg.requires_reinstall:
        return []  # it would (correctly) be refused for extraction
    if cfg.github and not hasattr(pkg, "OPTIONAL_RELEASE_KEYS"):
        # An app without GitHub updates can't read release.json's source keys: nothing to try.
        return ["Trial extraction skipped: this checkout's app doesn't know GitHub release.json keys."]
    env = Environment(python=cfg.python, bootstrap_version=cfg.bootstrap_version)
    try:
        pkg.extract_payload(path, current, tmp / "trial-versions", trusted_keys=previous_keys, env=env)
    except (pkg.UpdateRejected, pkg.NotInstallable, pkg.ExtractError) as exc:
        raise BuildError(f"Self-check extraction failed: {exc}") from None
    return []


def read_passphrase_stdin(stream=None) -> str:
    """One line from standard input (the GitHub workflow pipes the secret in): never from a
    command line or the environment, where it could show up in process lists or logs."""
    stream = stream if stream is not None else sys.stdin
    line = stream.readline(MAX_PASSPHRASE_STDIN + 1)
    if len(line) > MAX_PASSPHRASE_STDIN:
        raise BuildError("The passphrase on standard input is too long.")
    text = line.rstrip("\r\n")
    if not text:
        raise BuildError("No passphrase on standard input.")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a signed Iron Owl update (.ftupdate).", allow_abbrev=False)
    parser.add_argument("--version", required=True)
    parser.add_argument("--kind", choices=("app", "full"), default="app")
    parser.add_argument("--min-current-version", default="1.0.0")
    parser.add_argument("--requires-reinstall", action="store_true")
    parser.add_argument("--bootstrap-version", type=int, default=1)
    parser.add_argument("--key", type=Path, default=keygen.default_key_path())
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--commit", action="store_true", help='commit "Release X.Y.Z" and tag vX.Y.Z')
    parser.add_argument("--source", choices=SOURCES, default="file",
                        help="file: emailed updates (default); github: public GitHub releases")
    parser.add_argument("--update-repo", default=None, help="github: <owner>/<name> the app checks for updates")
    parser.add_argument("--tag", default=None, help="github: the release tag (vX.Y.Z) HEAD must be")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="read the signing key passphrase from standard input (CI)")
    steps = parser.add_mutually_exclusive_group()
    steps.add_argument("--unsigned-out", type=Path, default=None,
                       help="github step 1: build and write the unsigned manifest + payload here (no key)")
    steps.add_argument("--sign-only", type=Path, default=None, metavar="FOLDER",
                       help="github step 2: sign the folder step 1 made (builds nothing)")
    args = parser.parse_args(argv)
    cfg = BuildConfig(
        repo_root=TOOL_REPO_ROOT, version=args.version, kind=args.kind, out_dir=args.out, key_path=args.key,
        min_current_version=args.min_current_version, requires_reinstall=args.requires_reinstall,
        bootstrap_version=args.bootstrap_version, commit=args.commit,
        source=args.source, update_repo=args.update_repo, tag=args.tag, unsigned_out=args.unsigned_out,
    )
    try:
        if args.unsigned_out is not None and args.passphrase_stdin:
            raise BuildError("--unsigned-out uses no key: leave out --passphrase-stdin.")
        passphrase = None
        if args.passphrase_stdin:
            secret = read_passphrase_stdin()
            passphrase = lambda: secret  # noqa: E731
        if args.sign_only is not None:
            result = sign_only(cfg, args.sign_only, passphrase=passphrase)
        else:
            result = build(cfg, passphrase=passphrase)  # else typed (getpass); tests inject passphrase=
    except BuildError as exc:
        print(f"\nRELEASE NOT BUILT: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    if cfg.unsigned_out is not None:
        print(f"\nUnsigned update written to {result.package} ({result.size / MB:.2f} MB payload): "
              "sign it with --sign-only.")
        return 0
    print(f"\nBuilt {result.package} ({result.size / MB:.2f} MB, {len(result.payload_names)} files)")
    print(f"SHA-256 {result.sha256}")
    if cfg.github:
        return 0
    print(f"Email text: {result.package.parent / 'EMAIL.txt'}")
    if not cfg.commit:
        print(f'Next: git commit -am "Release {cfg.version}" && git tag -a v{cfg.version} -m "Iron Owl {cfg.version}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
