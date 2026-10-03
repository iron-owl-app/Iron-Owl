"""The runtime lock file: every package (with its dependencies) pinned with a SHA-256 hash.

build_package.ps1 and build_update.py (``--kind full``) install the packaged runtime from
``tools/release/requirements-lock.txt`` with ``make_lock.py install-runtime``, so a package can
only ship the exact files that were locked. The lock is made from ``backend/requirements.txt``
for the packaged target (CPython 3.11, win_amd64).

Wheels only, with one exception: the packages in ``SDIST_ALLOWLIST`` publish nothing but an
sdist on PyPI (plaid-python: every recent version), so the lock pins that sdist's sha256 and
names it in a ``# sdist: <name>`` header line. Any other package without a wheel is refused.
``tools/release/build-lock.txt`` pins (hash-checked wheels) the build backend those sdists
need (``BUILD_REQUIREMENTS``), with the same deps_id header.

Making or refreshing the locks (needs the network, PyPI only; run it after changing
backend/requirements.txt, then commit both files with that change):

    backend\\.venv\\Scripts\\python.exe tools\\release\\make_lock.py generate

It resolves the build backend (wheels only), installs it hash-checked into a fresh venv, reads
each allowlisted sdist's sha256 from pip, builds those sdists into wheels exactly as the build
does, then asks pip to resolve the requirements for the target with those wheels on
``--find-links`` and everything else wheels-only, without installing anything
(``pip install --dry-run --report``; pip 22.2 or newer). The header records the
requirements' deps_id: the build refuses a lock whose deps_id doesn't match requirements.txt.

Installing a runtime (build_package.ps1, build_update.py):

    make_lock.py install-runtime <lock> <site-packages> [--python-version 3.11] [--wheel-dir <dir>]
        1. a fresh venv (``python -m venv``) with build-lock.txt installed into it
           (``--require-hashes --no-deps --only-binary=:all:``), so no unpinned build
           dependency is ever downloaded;
        2. ``pip wheel --no-cache-dir --no-deps --require-hashes --no-build-isolation
           --no-binary=:all:`` of the lock's allowlisted sdists (hash-checked) in that venv; each
           must give one pure Python ``py3-none-any`` wheel;
        3. ``pip install --require-hashes --no-deps --only-binary=:all:`` for the target from
           the lock, with ``--find-links`` at those wheels: an sdist's pin takes the sha256 of
           the wheel just built from it (a copy of the lock in a temp folder; the committed
           lock keeps the sdist's).
        ``--wheel-dir`` (offline builds): every pip step uses only that folder
        (``--no-index --find-links``); it must hold the wheels and the sdists.

Checks (no network):

    make_lock.py lock-id <lock>                       print the deps_id the lock was made for
    make_lock.py check-installed <installed.txt> <lock or requirements file>
        exit 1 (listing the differences) unless installed.txt (``pip list --format freeze``
        of a runtime) has exactly the lock's packages and versions; against a plain
        requirements file, every pin in it must be installed at that version.
    make_lock.py subset <lock> <name>...
        print only those packages' lines (pin + hash) from the lock, for
        ``pip install --require-hashes --no-deps -r`` (the release workflow's signing job
        installs just cryptography this way); exit 1 if one is missing or is an sdist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_REQUIREMENTS = REPO / "backend" / "requirements.txt"
DEFAULT_LOCK = HERE / "requirements-lock.txt"
BUILD_LOCK_NAME = "build-lock.txt"  # always beside the runtime lock

sys.path.insert(0, str(HERE))
import deps_id as _deps  # noqa: E402

# Packages that publish only an sdist and may be built into a wheel at build time. Keep it short:
# everything else must have a cp311 win_amd64 (or pure Python) wheel.
SDIST_ALLOWLIST = frozenset({"plaid-python"})
# The build backend for those sdists. plaid-python has a setup.py and no pyproject.toml, so pip
# builds it with setuptools' default backend (setuptools.build_meta:__legacy__); setuptools 70.1
# and newer make wheels by themselves, so the separate "wheel" package isn't needed.
BUILD_REQUIREMENTS = ("setuptools==84.0.0",)

PIP_TARGET_ARGS = ("--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.11",
                   "--implementation", "cp")
_HEADER_ID_RE = re.compile(r"^# deps_id: ([0-9a-f]{16})\s*$", re.MULTILINE)
_SDIST_HEADER_RE = re.compile(r"^# sdist: ([a-z0-9][a-z0-9-]*)\s*$", re.MULTILINE)
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;\\]+)")
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SDIST_SUFFIXES = (".tar.gz", ".zip")

Run = Callable[..., Any]  # subprocess.run's shape (tests pass a fake)


def canonical(name: str) -> str:
    """PEP 503 project name ("Pydantic_Core" -> "pydantic-core")."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(text: str) -> dict[str, str]:
    """{canonical name: version} of the ``name==version`` lines (hash lines, options and
    comments are skipped)."""
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _PIN_RE.match(line)
        if m:
            pins[canonical(m.group(1))] = m.group(2)
    return pins


def parse_entries(text: str) -> dict[str, tuple[str, list[str]]]:
    """{canonical name: (version, [sha256, ...])}: a lock file's pins and their hash lines."""
    entries: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            current = None
            continue
        m = _PIN_RE.match(line)
        if m:
            current = canonical(m.group(1))
            entries[current] = (m.group(2), [])
        if current is not None:
            entries[current][1].extend(_HASH_RE.findall(line))
    return entries


def lock_id(lock_text: str) -> str | None:
    m = _HEADER_ID_RE.search(lock_text)
    return m.group(1) if m else None


def lock_sdists(lock_text: str) -> list[str]:
    """The ``# sdist: <name>`` header names (packages built from their sdist at build time)."""
    return sorted(set(_SDIST_HEADER_RE.findall(lock_text)))


def _render(header: Sequence[str], entries: Mapping[str, tuple[str, str]]) -> str:
    lines = [*header, ""] if header else []
    for key in sorted(entries):
        version, sha = entries[key]
        lines.append(f"{key}=={version} \\")
        lines.append(f"    --hash=sha256:{sha}")
    return "\n".join(lines) + "\n"


def _report_items(report: Mapping[str, Any]) -> list[tuple[str, str, str | None, str]]:
    """(name, version, sha256 or None, download url) of each entry of a pip report."""
    items = []
    for item in report.get("install", []):
        meta = item.get("metadata") or {}
        name, version = meta.get("name"), meta.get("version")
        info = item.get("download_info") or {}
        sha = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
        url = info.get("url")
        if not (isinstance(name, str) and isinstance(version, str)):
            raise ValueError("pip report entry without a name or version")
        items.append((name, version, sha if isinstance(sha, str) else None, url if isinstance(url, str) else ""))
    return items


def _url_file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _is_index_wheel(url: str) -> bool:
    return urlsplit(url).scheme == "https" and _url_file_name(url).endswith(".whl")


def lock_text_from_report(report: Mapping[str, Any], requirements_text: str,
                          sdists: Mapping[str, tuple[str, str]] | None = None) -> str:
    """The lock file for a ``pip install --dry-run --report`` result.

    ``sdists``: {canonical name: (version, sdist sha256)} for the allowlisted packages that were
    built into wheels for the resolve; their report entry must be that local pure Python wheel,
    and the lock pins the sdist's hash. Every other entry must be a wheel from the index."""
    sdists = dict(sdists or {})
    bad = sorted(set(sdists) - SDIST_ALLOWLIST)
    if bad:
        raise ValueError(f"not allowed to build from source: {', '.join(bad)} (SDIST_ALLOWLIST)")
    entries: dict[str, tuple[str, str]] = {}
    for name, version, sha, url in _report_items(report):
        key = canonical(name)
        if key in sdists:
            want_version, sdist_sha = sdists[key]
            if version != want_version:
                raise ValueError(f"{key}: built {version}, expected {want_version}")
            if not (url.startswith("file:") and _url_file_name(url).endswith("-py3-none-any.whl")):
                raise ValueError(f"{key}: expected the locally built py3-none-any wheel, got {url or 'nothing'}")
            if not _SHA256_RE.fullmatch(sdist_sha):
                raise ValueError(f"no sha256 for the {key} {version} sdist")
            entries[key] = (version, sdist_sha)
            continue
        if not _is_index_wheel(url):
            raise ValueError(f"{key} {version} is not a wheel from the index ({_url_file_name(url) or 'no file'}); "
                             f"only {', '.join(sorted(SDIST_ALLOWLIST))} may be built from source")
        if not (sha and _SHA256_RE.fullmatch(sha)):
            raise ValueError(f"no sha256 for {name} {version} in the pip report")
        entries[key] = (version, sha)
    if not entries:
        raise ValueError("pip resolved nothing")
    wanted = parse_pins(requirements_text)
    missing = sorted((set(wanted) | set(sdists)) - set(entries))
    if missing:
        raise ValueError(f"not in the pip report: {', '.join(missing)}")
    wrong = sorted(n for n, v in wanted.items() if entries[n][0] != v)
    if wrong:
        raise ValueError(f"resolved to other versions than pinned: {', '.join(wrong)}")
    target = "Target: CPython 3.11, win_amd64, wheels only"
    if sdists:
        target += f" (except the sdists named below, built into wheels at build time with {BUILD_LOCK_NAME})"
    header = [
        "# FinTrack runtime lock: generated by tools/release/make_lock.py - do not edit by hand.",
        f"# {target}. Source: backend/requirements.txt",
        *(f"# sdist: {key}" for key in sorted(sdists)),
        f"# deps_id: {_deps.deps_id(requirements_text)}",
    ]
    return _render(header, entries)


def build_lock_text_from_report(report: Mapping[str, Any], build_requirements: Iterable[str],
                                runtime_deps_id: str, for_sdists: Iterable[str]) -> str:
    """build-lock.txt: the build backend for the allowlisted sdists, hash-pinned wheels only."""
    entries: dict[str, tuple[str, str]] = {}
    for name, version, sha, url in _report_items(report):
        if not _is_index_wheel(url):
            raise ValueError(f"build backend {name} {version} is not a wheel from the index")
        if not (sha and _SHA256_RE.fullmatch(sha)):
            raise ValueError(f"no sha256 for {name} {version} in the pip report")
        entries[canonical(name)] = (version, sha)
    wanted = parse_pins("\n".join(build_requirements))
    wrong = sorted(n for n, v in wanted.items() if n not in entries or entries[n][0] != v)
    if wrong or not wanted:
        raise ValueError(f"build backend not resolved as pinned: {', '.join(wrong) or 'no pins'}")
    header = [
        "# FinTrack build-backend lock: generated by tools/release/make_lock.py - do not edit by hand.",
        "# Installed (wheels only, hash-checked) into a fresh venv that builds these sdists from",
        f"# requirements-lock.txt into wheels: {', '.join(sorted(for_sdists))}.",
        f"# deps_id: {runtime_deps_id}",
    ]
    return _render(header, entries)


def sdist_sha_from_report(report: Mapping[str, Any], name: str, version: str) -> str:
    """The sha256 pip reports for one package's sdist (a ``--no-deps --no-binary=:all:`` dry run)."""
    items = _report_items(report)
    if len(items) != 1:
        raise ValueError(f"expected one entry for the {name} sdist, got {len(items)}")
    got_name, got_version, sha, url = items[0]
    if canonical(got_name) != canonical(name) or got_version != version:
        raise ValueError(f"expected the {name} {version} sdist, got {got_name} {got_version}")
    if not (urlsplit(url).scheme == "https" and _url_file_name(url).endswith(_SDIST_SUFFIXES)):
        raise ValueError(f"{name} {version}: not an sdist from the index ({_url_file_name(url) or 'no file'})")
    if not (sha and _SHA256_RE.fullmatch(sha)):
        raise ValueError(f"no sha256 for the {name} {version} sdist")
    return sha


def check_installed(installed_text: str, expected_text: str, *, exact: bool | None = None) -> list[str]:
    """Differences between a runtime's installed.txt and a lock (exact) or requirements
    file (every pin present at that version). Empty when they match. ``exact`` defaults to
    whether ``expected_text`` has a deps_id header (a lock)."""
    installed = parse_pins(installed_text)
    expected = parse_pins(expected_text)
    if exact is None:
        exact = lock_id(expected_text) is not None
    problems: list[str] = []
    for name, version in sorted(expected.items()):
        have = installed.get(name)
        if have is None:
            problems.append(f"missing {name}=={version}")
        elif have != version:
            problems.append(f"{name}: installed {have}, expected {version}")
    if exact:
        for name in sorted(set(installed) - set(expected)):
            problems.append(f"not in the lock: {name}=={installed[name]}")
    if not expected:
        problems.append("nothing to compare against (no pins)")
    return problems


def lock_subset(lock_text: str, names: list[str]) -> str:
    """The lock's ``name==version`` line and its hash lines for each of ``names`` (wheels only:
    an sdist's hash would never match a wheel)."""
    wanted = {canonical(n) for n in names}
    built = sorted(wanted & set(lock_sdists(lock_text)))
    if built:
        raise ValueError(f"built from source, not a wheel: {', '.join(built)}")
    out: list[str] = []
    keep = False
    for raw in lock_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            keep = False
            continue
        m = _PIN_RE.match(line)
        if m:
            keep = canonical(m.group(1)) in wanted
        if keep:
            out.append(raw.rstrip())
    found = parse_pins("\n".join(out))
    missing = sorted(wanted - set(found))
    if missing:
        raise ValueError(f"not in the lock: {', '.join(missing)}")
    for i, line in enumerate(out):  # every pin is followed by its hash line
        if _PIN_RE.match(line.strip()) and not (i + 1 < len(out) and "--hash=sha256:" in out[i + 1]):
            raise ValueError("a package in the lock has no hash")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------- building the allowlisted sdists


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _index_args(wheel_dir: Path | None) -> list[str]:
    return ["--no-index", "--find-links", str(wheel_dir)] if wheel_dir else []


def venv_python(venv: Path) -> Path:
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def lock_sdist_pins(lock_text: str) -> dict[str, tuple[str, str]]:
    """{name: (version, sdist sha256)} for the lock's ``# sdist:`` packages; refuses any name
    outside SDIST_ALLOWLIST and any without exactly one hash."""
    names = lock_sdists(lock_text)
    bad = sorted(set(names) - SDIST_ALLOWLIST)
    if bad:
        raise ValueError(f"the lock builds {', '.join(bad)} from source, which SDIST_ALLOWLIST doesn't allow")
    entries = parse_entries(lock_text)
    pins: dict[str, tuple[str, str]] = {}
    for name in names:
        if name not in entries or len(entries[name][1]) != 1:
            raise ValueError(f"the lock has no single sha256 for the {name} sdist")
        pins[name] = (entries[name][0], entries[name][1][0])
    return pins


def make_build_env(python: str, build_lock: Path, runtime_deps_id: str | None, work: Path, *,
                   run: Run = subprocess.run, wheel_dir: Path | None = None) -> Path:
    """A fresh venv with the hash-pinned build backend installed; returns its python."""
    text = build_lock.read_text(encoding="utf-8")
    if runtime_deps_id is None or lock_id(text) != runtime_deps_id:
        raise ValueError(f"{build_lock.name} doesn't belong to this lock (deps_id {lock_id(text)}, "
                         f"lock {runtime_deps_id}): run make_lock.py generate")
    pins = parse_entries(text)
    if not pins or any(len(hashes) != 1 for _, hashes in pins.values()):
        raise ValueError(f"{build_lock.name} must pin every package with one sha256")
    venv = work / "build-env"
    run([python, "-m", "venv", str(venv)], check=True)
    vpy = venv_python(venv)
    run([str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "--no-deps",
         "--only-binary=:all:", *_index_args(wheel_dir), "-r", str(build_lock)], check=True)
    listed = run([str(vpy), "-m", "pip", "list", "--disable-pip-version-check", "--format", "freeze"],
                 check=True, capture_output=True, text=True)
    problems = check_installed(listed.stdout or "", text, exact=False)
    if problems:
        raise ValueError(f"the build venv doesn't match {build_lock.name}: {'; '.join(problems)}")
    return vpy


def build_sdist_wheels(vpy: Path, sdists: Mapping[str, tuple[str, str]], work: Path, *,
                       run: Run = subprocess.run, wheel_dir: Path | None = None) -> dict[str, Path]:
    """Build each allowlisted sdist (hash-checked) into a pure Python wheel with the build venv's
    pinned backend; returns {name: wheel file}."""
    bad = sorted(set(sdists) - SDIST_ALLOWLIST)
    if bad:
        raise ValueError(f"not allowed to build from source: {', '.join(bad)}")
    out = work / "built-wheels"
    out.mkdir(parents=True, exist_ok=True)
    reqs = work / "sdists.txt"
    reqs.write_text(_render([], sdists), encoding="utf-8")
    # --no-cache-dir: pip's wheel cache could hand back a wheel built earlier by something else.
    run([str(vpy), "-m", "pip", "wheel", "--disable-pip-version-check", "--no-cache-dir", "--no-deps",
         "--require-hashes", "--no-build-isolation", "--no-binary=:all:", *_index_args(wheel_dir),
         "--wheel-dir", str(out), "-r", str(reqs)], check=True)
    expected = {f"{name.replace('-', '_')}-{version}-py3-none-any.whl": name for name, (version, _) in sdists.items()}
    wheels: dict[str, Path] = {}
    for path in sorted(out.iterdir()):
        name = expected.get(path.name)
        if name is None:
            raise ValueError(f"unexpected build output {path.name} (each sdist must give one py3-none-any wheel)")
        wheels[name] = path
    missing = sorted(set(sdists) - set(wheels))
    if missing:
        raise ValueError(f"no py3-none-any wheel was built for {', '.join(missing)}")
    return wheels


def lock_with_wheel_hashes(lock_text: str, wheel_hashes: Mapping[str, str]) -> str:
    """The lock as a plain requirements file, with each built sdist's pin carrying the sha256
    of the wheel built from it."""
    out: dict[str, tuple[str, str]] = {}
    for name, (version, hashes) in parse_entries(lock_text).items():
        if name in wheel_hashes:
            out[name] = (version, wheel_hashes[name])
        elif len(hashes) == 1:
            out[name] = (version, hashes[0])
        else:
            raise ValueError(f"{name} must have exactly one sha256 in the lock")
    missing = sorted(set(wheel_hashes) - set(out))
    if missing:
        raise ValueError(f"not in the lock: {', '.join(missing)}")
    if not out:
        raise ValueError("the lock has no packages")
    return _render([], out)


def install_runtime(python: str, lock: Path, target: Path, *, python_version: str = "3.11",
                    wheel_dir: Path | None = None, run: Run = subprocess.run) -> None:
    """Install the lock into ``target`` (see the module docstring, install-runtime)."""
    text = lock.read_text(encoding="utf-8")
    sdists = lock_sdist_pins(text)
    with tempfile.TemporaryDirectory(prefix="ftlock-") as tmp:
        work = Path(tmp)
        links: list[str] = []
        hashes: dict[str, str] = {}
        if sdists:
            vpy = make_build_env(python, lock.with_name(BUILD_LOCK_NAME), lock_id(text), work,
                                 run=run, wheel_dir=wheel_dir)
            wheels = build_sdist_wheels(vpy, sdists, work, run=run, wheel_dir=wheel_dir)
            hashes = {name: _sha256_file(path) for name, path in wheels.items()}
            links = ["--find-links", str(work / "built-wheels")]
        reqs = work / "runtime-lock.txt"
        reqs.write_text(lock_with_wheel_hashes(text, hashes), encoding="utf-8")
        run([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
             "--only-binary=:all:", "--platform", "win_amd64", "--python-version", python_version,
             "--implementation", "cp", "--target", str(target), "--require-hashes", "--no-deps",
             *links, *_index_args(wheel_dir), "-r", str(reqs)], check=True)


# --------------------------------------------------------------- generate


def _dry_run_report(run: Run, python: str, args: Sequence[str], work: Path, tag: str) -> dict[str, Any]:
    report_path = work / f"report-{tag}.json"
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--dry-run",
         "--ignore-installed", *args, "--report", str(report_path)], check=True)
    return json.loads(report_path.read_text(encoding="utf-8"))


def _write(path: Path, text: str) -> None:
    tmp_out = path.with_name(path.name + ".tmp")
    tmp_out.write_text(text, encoding="utf-8", newline="\n")
    tmp_out.replace(path)


def generate(requirements: Path, out: Path, *, run: Run = subprocess.run) -> int:
    text = requirements.read_text(encoding="utf-8")
    _deps.normalized_requirements(text)  # same rules as deps_id: no -r/-c/--index-url
    the_id = _deps.deps_id(text)
    sdist_pins = {n: v for n, v in parse_pins(text).items() if n in SDIST_ALLOWLIST}
    build_out = out.with_name(BUILD_LOCK_NAME)
    build_text: str | None = None
    with tempfile.TemporaryDirectory(prefix="ftlock-") as tmp:
        work = Path(tmp)
        sdists: dict[str, tuple[str, str]] = {}
        links: list[str] = []
        if sdist_pins:
            # 1. the build backend, wheels only, hash-pinned
            breqs = work / "build-requirements.txt"
            breqs.write_text("\n".join(BUILD_REQUIREMENTS) + "\n", encoding="utf-8")
            report = _dry_run_report(run, sys.executable, [*PIP_TARGET_ARGS, "--target", str(work / "tb"),
                                                           "-r", str(breqs)], work, "build")
            build_text = build_lock_text_from_report(report, BUILD_REQUIREMENTS, the_id, sdist_pins)
            build_lock = work / BUILD_LOCK_NAME
            build_lock.write_text(build_text, encoding="utf-8")
            # 2. a venv with exactly that backend; each sdist's sha256 as pip downloads it
            vpy = make_build_env(sys.executable, build_lock, the_id, work, run=run)
            for name, version in sorted(sdist_pins.items()):
                report = _dry_run_report(run, str(vpy), ["--no-deps", "--no-binary=:all:", "--no-build-isolation",
                                                         f"{name}=={version}"], work, name)
                sdists[name] = (version, sdist_sha_from_report(report, name, version))
            # 3. built as the build does, so the resolve sees their real dependencies
            build_sdist_wheels(vpy, sdists, work, run=run)
            links = ["--find-links", str(work / "built-wheels")]
        try:
            report = _dry_run_report(run, sys.executable, [*PIP_TARGET_ARGS, *links, "--target", str(work / "t"),
                                                           "-r", str(requirements)], work, "runtime")
        except subprocess.CalledProcessError:
            print("pip could not resolve the requirements as wheels for CPython 3.11 win_amd64. A package "
                  f"without a wheel? Only {', '.join(sorted(SDIST_ALLOWLIST))} may be built from source "
                  "(SDIST_ALLOWLIST in tools/release/make_lock.py).", file=sys.stderr)
            return 1
    lock = lock_text_from_report(report, text, sdists)
    if build_text is not None:
        _write(build_out, build_text)
        print(f"wrote {build_out} ({len(parse_pins(build_text))} packages)")
    elif build_out.exists():
        build_out.unlink()  # nothing to build from source any more
        print(f"removed {build_out} (no sdists to build)")
    _write(out, lock)
    built = f", built from source: {', '.join(sorted(sdists))}" if sdists else ""
    print(f"wrote {out} ({len(parse_pins(lock))} packages, deps_id {lock_id(lock)}{built})")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["generate"] and len(argv) in (1, 3):
        requirements, out = DEFAULT_REQUIREMENTS, DEFAULT_LOCK
        if len(argv) == 3:
            requirements, out = Path(argv[1]), Path(argv[2])
        return generate(requirements, out)
    if argv[:1] == ["install-runtime"]:
        parser = argparse.ArgumentParser(prog="make_lock.py install-runtime")
        parser.add_argument("lock", type=Path)
        parser.add_argument("target", type=Path)
        parser.add_argument("--python-version", default="3.11")
        parser.add_argument("--wheel-dir", type=Path)
        args = parser.parse_args(argv[1:])
        if not re.fullmatch(r"3\.\d{1,2}", args.python_version):
            print("--python-version must look like 3.11", file=sys.stderr)
            return 2
        try:
            install_runtime(sys.executable, args.lock, args.target, python_version=args.python_version,
                            wheel_dir=args.wheel_dir)
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            print(f"install-runtime failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if argv[:1] == ["lock-id"] and len(argv) == 2:
        value = lock_id(Path(argv[1]).read_text(encoding="utf-8"))
        if value is None:
            print("the lock file has no deps_id header", file=sys.stderr)
            return 1
        print(value)
        return 0
    if argv[:1] == ["check-installed"] and len(argv) == 3:
        problems = check_installed(Path(argv[1]).read_text(encoding="utf-8"),
                                   Path(argv[2]).read_text(encoding="utf-8"))
        for line in problems:
            print(line, file=sys.stderr)
        return 1 if problems else 0
    if argv[:1] == ["subset"] and len(argv) >= 3:
        try:
            sys.stdout.write(lock_subset(Path(argv[1]).read_text(encoding="utf-8"), argv[2:]))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
