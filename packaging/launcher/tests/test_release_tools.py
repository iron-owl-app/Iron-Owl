"""tools/release/deps_id.py: stable runtime ids for runtimes/<deps_id>/site-packages."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("deps_id", REPO / "tools" / "release" / "deps_id.py")
deps_id = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps_id)


def test_id_shape_and_repo_requirements():
    value = deps_id.deps_id((REPO / "backend" / "requirements.txt").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{16}", value)


def test_id_ignores_order_case_spacing_and_comments():
    a = "fastapi==0.141.1\nuvicorn[standard]==0.54.0\n"
    b = "# runtime\n\nUvicorn[standard] == 0.54.0  # server\nFastAPI==0.141.1\n"
    assert deps_id.deps_id(a) == deps_id.deps_id(b)


def test_id_changes_with_versions_and_target():
    a = "fastapi==0.141.1\n"
    assert deps_id.deps_id(a) != deps_id.deps_id("fastapi==0.141.2\n")
    assert deps_id.deps_id(a) != deps_id.deps_id(a, target="cp312-win_amd64")


def test_dev_requirements_are_not_part_of_the_runtime():
    runtime = (REPO / "backend" / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "pytest" not in runtime and "httpx" not in runtime
    dev = (REPO / "backend" / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in dev


def test_options_are_refused():
    with pytest.raises(ValueError):
        deps_id.deps_id("-r other.txt\n")


# ------------------------------------------------------------------- runtime lock file

_lspec = importlib.util.spec_from_file_location("make_lock", REPO / "tools" / "release" / "make_lock.py")
make_lock = importlib.util.module_from_spec(_lspec)
_lspec.loader.exec_module(make_lock)

REQS = "fastapi==0.141.1\nuvicorn[standard]==0.54.0\n"


def _entry(name, version, sha="ab" * 32):
    return {"metadata": {"name": name, "version": version},
            "download_info": {"url": "https://files.example/x.whl", "archive_info": {"hashes": {"sha256": sha}}}}


def _report():
    return {"install": [_entry("fastapi", "0.141.1"), _entry("uvicorn", "0.54.0", "cd" * 32),
                        _entry("Pydantic_Core", "2.41.0", "ef" * 32)]}


def test_lock_from_a_pip_report_pins_everything_with_hashes():
    text = make_lock.lock_text_from_report(_report(), REQS)
    assert make_lock.lock_id(text) == deps_id.deps_id(REQS)
    assert "pydantic-core==2.41.0 \\\n    --hash=sha256:" + "ef" * 32 in text
    assert make_lock.parse_pins(text) == {"fastapi": "0.141.1", "uvicorn": "0.54.0", "pydantic-core": "2.41.0"}
    for line in text.splitlines():
        assert line.startswith(("#", "    --hash=sha256:")) or line == "" or "==" in line


def test_lock_refuses_a_report_without_hashes_or_with_other_versions():
    bad = _report()
    bad["install"][0]["download_info"]["archive_info"]["hashes"] = {"md5": "x"}
    with pytest.raises(ValueError):
        make_lock.lock_text_from_report(bad, REQS)
    other = _report()
    other["install"][0]["metadata"]["version"] = "0.141.2"
    with pytest.raises(ValueError):
        make_lock.lock_text_from_report(other, REQS)
    with pytest.raises(ValueError):
        make_lock.lock_text_from_report({"install": [_entry("fastapi", "0.141.1")]}, REQS)


def test_installed_runtime_must_match_the_lock_exactly():
    lock = make_lock.lock_text_from_report(_report(), REQS)
    good = "fastapi==0.141.1\nuvicorn==0.54.0\npydantic_core==2.41.0\n"
    assert make_lock.check_installed(good, lock) == []
    assert make_lock.check_installed("fastapi==0.141.1\nuvicorn==0.54.0\n", lock) == ["missing pydantic-core==2.41.0"]
    assert make_lock.check_installed(good.replace("2.41.0", "2.40.0"), lock) == [
        "pydantic-core: installed 2.40.0, expected 2.41.0"]
    assert make_lock.check_installed(good + "evil==1.0\n", lock) == ["not in the lock: evil==1.0"]


def test_without_a_lock_the_requirements_pins_must_be_installed():
    installed = "fastapi==0.141.1\nuvicorn==0.54.0\nh11==0.16.0\n"
    assert make_lock.check_installed(installed, REQS) == []  # extra dependencies are fine here
    assert make_lock.check_installed("fastapi==0.141.1\n", REQS) == ["missing uvicorn==0.54.0"]
    assert make_lock.check_installed(installed, "# nothing\n") != []


def test_lock_cli(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text(make_lock.lock_text_from_report(_report(), REQS), encoding="utf-8")
    installed = tmp_path / "installed.txt"
    installed.write_text("fastapi==0.141.1\nuvicorn==0.54.0\npydantic-core==2.41.0\n", encoding="utf-8")
    assert make_lock.main(["check-installed", str(installed), str(lock)]) == 0
    installed.write_text("fastapi==0.141.1\n", encoding="utf-8")
    assert make_lock.main(["check-installed", str(installed), str(lock)]) == 1
    assert make_lock.main(["lock-id", str(lock)]) == 0
    assert make_lock.main(["lock-id", str(installed)]) == 1
    assert make_lock.main(["bogus"]) == 2


def test_committed_locks_match_the_requirements():
    """The release workflow builds with -RequireLock: both files must be committed and current."""
    lock = REPO / "tools" / "release" / "requirements-lock.txt"
    requirements = (REPO / "backend" / "requirements.txt").read_text(encoding="utf-8")
    text = lock.read_text(encoding="utf-8")
    assert make_lock.lock_id(text) == deps_id.deps_id(requirements)
    assert make_lock.check_installed(text, requirements) == []
    entries = make_lock.parse_entries(text)
    assert entries and all(len(hashes) == 1 for _, hashes in entries.values())
    sdists = make_lock.lock_sdist_pins(text)  # refuses anything outside the allowlist
    assert set(sdists) == {"plaid-python"} and sdists["plaid-python"][0] == entries["plaid-python"][0]
    build = (REPO / "tools" / "release" / make_lock.BUILD_LOCK_NAME).read_text(encoding="utf-8")
    assert make_lock.lock_id(build) == make_lock.lock_id(text)
    assert make_lock.check_installed(build, "\n".join(make_lock.BUILD_REQUIREMENTS)) == []
    assert all(len(hashes) == 1 for _, hashes in make_lock.parse_entries(build).values())
    # the signing job's packages are wheels in the lock
    assert make_lock.lock_subset(text, ["cryptography", "cffi", "pycparser"]).count("--hash=sha256:") == 3


# ------------------------------------------------------------------- sdist allowlist

SDIST_SHA = "5d" * 32
PLAID_WHEEL_URL = "file:///C:/Temp/ftlock-x/built-wheels/plaid_python-44.0.0-py3-none-any.whl"
REQS_WITH_PLAID = REQS + "plaid-python==44.0.0\n"


def _plaid_report():
    report = _report()
    plaid = _entry("plaid-python", "44.0.0", "77" * 32)
    plaid["download_info"]["url"] = PLAID_WHEEL_URL
    report["install"].append(plaid)
    return report


def test_lock_pins_the_allowlisted_sdists_hash_not_the_local_wheels():
    text = make_lock.lock_text_from_report(_plaid_report(), REQS_WITH_PLAID, {"plaid-python": ("44.0.0", SDIST_SHA)})
    assert "# sdist: plaid-python\n" in text
    assert "plaid-python==44.0.0 \\\n    --hash=sha256:" + SDIST_SHA in text
    assert "77" * 32 not in text
    assert make_lock.lock_sdists(text) == ["plaid-python"]
    assert make_lock.lock_sdist_pins(text) == {"plaid-python": ("44.0.0", SDIST_SHA)}
    assert make_lock.lock_id(text) == deps_id.deps_id(REQS_WITH_PLAID)
    # a lock without sdists has no sdist header
    assert make_lock.lock_sdists(make_lock.lock_text_from_report(_report(), REQS)) == []


def _with_url(report, name, url):
    for item in report["install"]:
        if item["metadata"]["name"] == name:
            item["download_info"]["url"] = url
    return report


@pytest.mark.parametrize("name,url", [
    ("fastapi", "https://files.example/fastapi-0.141.1.tar.gz"),  # another sdist
    ("fastapi", "file:///C:/Temp/fastapi-0.141.1-py3-none-any.whl"),  # a local wheel nobody locked
    ("fastapi", "http://files.example/fastapi-0.141.1-py3-none-any.whl"),  # not https
    ("fastapi", ""),
])
def test_lock_refuses_anything_but_index_wheels_outside_the_allowlist(name, url):
    with pytest.raises(ValueError, match="not a wheel from the index"):
        make_lock.lock_text_from_report(_with_url(_report(), name, url), REQS)


def test_lock_refuses_sdists_outside_the_allowlist_and_odd_builds():
    sdists = {"plaid-python": ("44.0.0", SDIST_SHA)}
    with pytest.raises(ValueError, match="not allowed to build from source: fastapi"):
        make_lock.lock_text_from_report(_plaid_report(), REQS_WITH_PLAID, {**sdists, "fastapi": ("0.141.1", SDIST_SHA)})
    for url in ("https://files.example/plaid_python-44.0.0-py3-none-any.whl",  # not the local build
                "file:///C:/Temp/plaid_python-44.0.0-cp311-cp311-win_amd64.whl",  # not pure Python
                "file:///C:/Temp/plaid_python-44.0.0.tar.gz"):
        with pytest.raises(ValueError, match="locally built py3-none-any wheel"):
            make_lock.lock_text_from_report(_with_url(_plaid_report(), "plaid-python", url), REQS_WITH_PLAID, sdists)
    with pytest.raises(ValueError, match="built 44.0.0, expected 45.0.0"):
        make_lock.lock_text_from_report(_plaid_report(), REQS_WITH_PLAID, {"plaid-python": ("45.0.0", SDIST_SHA)})
    with pytest.raises(ValueError, match="no sha256"):
        make_lock.lock_text_from_report(_plaid_report(), REQS_WITH_PLAID, {"plaid-python": ("44.0.0", "x")})
    with pytest.raises(ValueError, match="not in the pip report: plaid-python"):
        make_lock.lock_text_from_report(_report(), REQS, sdists)
    # a hand-edited lock that builds something else from source
    text = make_lock.lock_text_from_report(_report(), REQS).replace("# deps_id:", "# sdist: fastapi\n# deps_id:")
    with pytest.raises(ValueError, match="SDIST_ALLOWLIST"):
        make_lock.lock_sdist_pins(text)
    assert make_lock.SDIST_ALLOWLIST == frozenset({"plaid-python"})


def test_subset_refuses_an_sdist():
    text = make_lock.lock_text_from_report(_plaid_report(), REQS_WITH_PLAID, {"plaid-python": ("44.0.0", SDIST_SHA)})
    with pytest.raises(ValueError, match="built from source"):
        make_lock.lock_subset(text, ["fastapi", "plaid_python"])
    assert make_lock.lock_subset(text, ["fastapi"]).count("--hash=") == 1


def _backend_report(version="84.0.0", url="https://files.example/setuptools-84.0.0-py3-none-any.whl"):
    entry = _entry("setuptools", version, "be" * 32)
    entry["download_info"]["url"] = url
    return {"install": [entry]}


def test_build_lock_pins_the_build_backend():
    text = make_lock.build_lock_text_from_report(_backend_report(), ["setuptools==84.0.0"], "0123456789abcdef",
                                                 ["plaid-python"])
    assert make_lock.lock_id(text) == "0123456789abcdef"
    assert make_lock.parse_entries(text) == {"setuptools": ("84.0.0", ["be" * 32])}
    assert "plaid-python" in text
    with pytest.raises(ValueError, match="not resolved as pinned: setuptools"):
        make_lock.build_lock_text_from_report(_backend_report("83.0.0"), ["setuptools==84.0.0"], "0" * 16, ["x"])
    with pytest.raises(ValueError, match="not a wheel"):
        make_lock.build_lock_text_from_report(_backend_report(url="https://files.example/setuptools-84.0.0.tar.gz"),
                                              ["setuptools==84.0.0"], "0" * 16, ["x"])
    # setuptools builds wheels itself (70.1+): the backend is setuptools alone, exactly pinned
    assert make_lock.BUILD_REQUIREMENTS == ("setuptools==84.0.0",)


def test_sdist_hash_comes_from_an_index_sdist():
    def report(url, name="plaid-python", version="44.0.0"):
        e = _entry(name, version, SDIST_SHA)
        e["download_info"]["url"] = url
        return {"install": [e]}

    good = "https://files.example/packages/plaid_python-44.0.0.tar.gz"
    assert make_lock.sdist_sha_from_report(report(good), "plaid-python", "44.0.0") == SDIST_SHA
    with pytest.raises(ValueError, match="not an sdist"):
        make_lock.sdist_sha_from_report(report(good.replace(".tar.gz", "-py3-none-any.whl")), "plaid-python", "44.0.0")
    with pytest.raises(ValueError, match="not an sdist"):
        make_lock.sdist_sha_from_report(report("file:///C:/x/plaid_python-44.0.0.tar.gz"), "plaid-python", "44.0.0")
    with pytest.raises(ValueError, match="expected the plaid-python 45.0.0"):
        make_lock.sdist_sha_from_report(report(good), "plaid-python", "45.0.0")
    with pytest.raises(ValueError, match="expected one entry"):
        make_lock.sdist_sha_from_report({"install": []}, "plaid-python", "44.0.0")


# ------------------------------------------------------------------- install-runtime (pip mocked)

WHEEL_BYTES = b"PK fake plaid wheel"


class FakePip:
    """Stands in for subprocess.run: records every command, makes the files pip would make,
    and keeps a copy of each -r file (they live in a temp folder that is gone afterwards)."""

    def __init__(self, *, wheel_name="plaid_python-44.0.0-py3-none-any.whl", listed="pip==24.0\nsetuptools==84.0.0\n",
                 reports=None):
        self.calls: list[list[str]] = []
        self.requirements: list[str] = []
        self.wheel_name, self.listed, self.reports = wheel_name, listed, reports or {}

    def __call__(self, cmd, check=False, capture_output=False, text=False):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        assert check is True
        if "-r" in cmd:
            self.requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8"))
        if cmd[1:3] == ["-m", "venv"]:
            Path(cmd[3]).mkdir(parents=True)
        elif cmd[2:4] == ["pip", "wheel"]:
            (Path(cmd[cmd.index("--wheel-dir") + 1]) / self.wheel_name).write_bytes(WHEEL_BYTES)
        elif cmd[2:4] == ["pip", "list"]:
            assert capture_output and text
            return subprocess.CompletedProcess(cmd, 0, stdout=self.listed, stderr="")
        elif "--report" in cmd:
            report = next(r for key, r in self.reports.items() if key in " ".join(cmd))
            Path(cmd[cmd.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)


def _write_locks(folder: Path, *, build_id=None) -> Path:
    lock = folder / "requirements-lock.txt"
    text = make_lock.lock_text_from_report(_plaid_report(), REQS_WITH_PLAID, {"plaid-python": ("44.0.0", SDIST_SHA)})
    lock.write_text(text, encoding="utf-8")
    build = make_lock.build_lock_text_from_report(_backend_report(), ["setuptools==84.0.0"],
                                                  build_id or make_lock.lock_id(text), ["plaid-python"])
    (folder / make_lock.BUILD_LOCK_NAME).write_text(build, encoding="utf-8")
    return lock


def test_install_runtime_builds_the_sdist_with_the_pinned_backend_then_installs_hash_checked(tmp_path):
    lock = _write_locks(tmp_path)
    run = FakePip()
    make_lock.install_runtime("py.exe", lock, tmp_path / "site", run=run)
    venv, backend, listed, wheel, install = run.calls
    # 1. a fresh venv with only the hash-pinned build backend
    assert venv[:3] == ["py.exe", "-m", "venv"]
    vpy = str(make_lock.venv_python(Path(venv[3])))
    assert backend[:4] == [vpy, "-m", "pip", "install"]
    for arg in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
        assert arg in backend, arg
    assert backend[-2:] == ["-r", str(tmp_path / "build-lock.txt")]
    assert listed[:4] == [vpy, "-m", "pip", "list"]
    # 2. the sdist, hash-checked, built in that venv without isolation (nothing else downloaded)
    assert wheel[:4] == [vpy, "-m", "pip", "wheel"]
    for arg in ("--no-deps", "--require-hashes", "--no-build-isolation", "--no-binary=:all:", "--no-cache-dir"):
        assert arg in wheel, arg
    assert "--find-links" not in wheel and "--no-index" not in wheel
    assert run.requirements[1] == f"plaid-python==44.0.0 \\\n    --hash=sha256:{SDIST_SHA}\n"
    built = wheel[wheel.index("--wheel-dir") + 1]
    # 3. the runtime: wheels only, hash-checked, the built wheel found with --find-links
    assert install[:4] == ["py.exe", "-m", "pip", "install"]
    for arg in ("--only-binary=:all:", "--require-hashes", "--no-deps", "--no-compile"):
        assert arg in install, arg
    assert install[install.index("--find-links") + 1] == built
    assert install[install.index("--target") + 1] == str(tmp_path / "site")
    assert install[install.index("--platform") + 1] == "win_amd64"
    assert install[install.index("--python-version") + 1] == "3.11"
    assert "--no-index" not in install and "--no-build-isolation" not in install
    runtime = make_lock.parse_entries(run.requirements[2])
    assert runtime["plaid-python"] == ("44.0.0", [hashlib.sha256(WHEEL_BYTES).hexdigest()])
    assert runtime["fastapi"] == ("0.141.1", ["ab" * 32]) and runtime["pydantic-core"] == ("2.41.0", ["ef" * 32])
    assert len(runtime) == 4
    assert lock.read_text(encoding="utf-8").count(SDIST_SHA) == 1  # the committed lock is untouched


def test_install_runtime_without_sdists_is_one_hash_checked_pip_call(tmp_path):
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text(make_lock.lock_text_from_report(_report(), REQS), encoding="utf-8")
    run = FakePip()
    make_lock.install_runtime("py.exe", lock, tmp_path / "site", python_version="3.12", run=run)
    (install,) = run.calls
    assert "--require-hashes" in install and "--only-binary=:all:" in install and "--find-links" not in install
    assert install[install.index("--python-version") + 1] == "3.12"
    assert make_lock.parse_entries(run.requirements[0]) == make_lock.parse_entries(lock.read_text(encoding="utf-8"))


def test_install_runtime_offline_wheel_dir_reaches_every_pip_step(tmp_path):
    lock = _write_locks(tmp_path)
    run = FakePip()
    make_lock.install_runtime("py.exe", lock, tmp_path / "site", wheel_dir=tmp_path / "wheels", run=run)
    _, backend, _, wheel, install = run.calls
    for cmd in (backend, wheel, install):
        assert "--no-index" in cmd and str(tmp_path / "wheels") in cmd


@pytest.mark.parametrize("problem", ["stale build lock", "no build lock", "backend not installed", "not pure python",
                                     "extra output"])
def test_install_runtime_refuses(tmp_path, problem):
    lock = _write_locks(tmp_path, build_id="0123456789abcdef" if problem == "stale build lock" else None)
    run = FakePip()
    if problem == "no build lock":
        (tmp_path / make_lock.BUILD_LOCK_NAME).unlink()
    elif problem == "backend not installed":
        run.listed = "pip==24.0\nsetuptools==65.5.0\n"
    elif problem == "not pure python":
        run.wheel_name = "plaid_python-44.0.0-cp311-cp311-win_amd64.whl"
    elif problem == "extra output":
        run.wheel_name = "evil-1.0-py3-none-any.whl"
    with pytest.raises((ValueError, OSError)):
        make_lock.install_runtime("py.exe", lock, tmp_path / "site", run=run)
    assert not any(c[2:4] == ["pip", "install"] and "--target" in c for c in run.calls)  # nothing installed
    if problem in ("stale build lock", "no build lock"):
        assert run.calls == []


def test_install_runtime_cli(tmp_path, monkeypatch, capsys):
    lock = _write_locks(tmp_path)
    seen = []
    monkeypatch.setattr(make_lock, "install_runtime", lambda *a, **kw: seen.append((a, kw)))
    assert make_lock.main(["install-runtime", str(lock), str(tmp_path / "s"), "--python-version", "3.11",
                           "--wheel-dir", str(tmp_path / "w")]) == 0
    assert seen[0][0][1:] == (lock, tmp_path / "s") and seen[0][1] == {"python_version": "3.11",
                                                                       "wheel_dir": tmp_path / "w"}
    assert make_lock.main(["install-runtime", str(lock), str(tmp_path / "s"), "--python-version", "3.11 -r x"]) == 2

    def boom(*a, **kw):
        raise ValueError("the lock builds evil from source")

    monkeypatch.setattr(make_lock, "install_runtime", boom)
    assert make_lock.main(["install-runtime", str(lock), str(tmp_path / "s")]) == 1
    assert "evil" in capsys.readouterr().err


# ------------------------------------------------------------------- generate (pip and the network mocked)


def test_generate_writes_both_locks(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(REQS_WITH_PLAID, encoding="utf-8")
    sdist = _entry("plaid-python", "44.0.0", SDIST_SHA)
    sdist["download_info"]["url"] = "https://files.example/plaid_python-44.0.0.tar.gz"
    run = FakePip(reports={"build-requirements.txt": _backend_report(), "--no-binary=:all:": {"install": [sdist]},
                           str(requirements): _plaid_report()})
    out = tmp_path / "requirements-lock.txt"
    assert make_lock.generate(requirements, out, run=run) == 0
    assert run.requirements[0] == "setuptools==84.0.0\n"
    text = out.read_text(encoding="utf-8")
    assert make_lock.lock_sdist_pins(text) == {"plaid-python": ("44.0.0", SDIST_SHA)}
    assert make_lock.lock_id(text) == deps_id.deps_id(REQS_WITH_PLAID)
    build = (tmp_path / "build-lock.txt").read_text(encoding="utf-8")
    assert make_lock.lock_id(build) == make_lock.lock_id(text)
    assert make_lock.parse_entries(build) == {"setuptools": ("84.0.0", ["be" * 32])}
    dry_runs = [c for c in run.calls if "--dry-run" in c]
    assert len(dry_runs) == 3
    backend_run, sdist_run, runtime_run = dry_runs
    assert "--only-binary=:all:" in backend_run and "win_amd64" in backend_run
    assert "--no-binary=:all:" in sdist_run and "--no-build-isolation" in sdist_run and "--no-deps" in sdist_run
    assert sdist_run[0] == str(make_lock.venv_python(Path(run.calls[1][3])))  # in the build venv
    assert "--only-binary=:all:" in runtime_run and "--find-links" in runtime_run
    assert "--no-binary=:all:" not in runtime_run


def test_generate_without_sdists_removes_a_stale_build_lock(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(REQS, encoding="utf-8")
    (tmp_path / "build-lock.txt").write_text("# stale\n", encoding="utf-8")
    run = FakePip(reports={str(requirements): _report()})
    out = tmp_path / "requirements-lock.txt"
    assert make_lock.generate(requirements, out, run=run) == 0
    assert not (tmp_path / "build-lock.txt").exists()
    assert make_lock.lock_sdists(out.read_text(encoding="utf-8")) == []
    assert len(run.calls) == 1 and "--find-links" not in run.calls[0]


def test_generate_explains_a_package_without_a_wheel(tmp_path, capsys):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(REQS, encoding="utf-8")

    def fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    out = tmp_path / "requirements-lock.txt"
    assert make_lock.generate(requirements, out, run=fail) == 1
    assert "Only plaid-python may be built from source" in capsys.readouterr().err
    assert not out.exists()


def test_runtime_requirements_pin_cryptography_like_master():
    runtime = (REPO / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography==46.0.7" in runtime.splitlines()


def test_lock_subset_keeps_only_the_named_packages_with_their_hashes(tmp_path, capsys):
    """The release workflow's signing job installs only cryptography (and its dependencies)
    from the lock, hash-checked."""
    lock = make_lock.lock_text_from_report(_report(), REQS)

    def entry(pin: str, sha: str) -> str:
        return f"{pin} \\\n    --hash=sha256:{sha}\n"

    sub = make_lock.lock_subset(lock, ["Pydantic_Core", "fastapi"])
    assert sub == entry("fastapi==0.141.1", "ab" * 32) + entry("pydantic-core==2.41.0", "ef" * 32)
    with pytest.raises(ValueError, match="not in the lock: evil"):
        make_lock.lock_subset(lock, ["fastapi", "evil"])
    with pytest.raises(ValueError, match="no hash"):
        make_lock.lock_subset("fastapi==0.141.1\n" + entry("uvicorn==0.54.0", "cd" * 32), ["fastapi"])
    path = tmp_path / "lock.txt"
    path.write_text(lock, encoding="utf-8")
    assert make_lock.main(["subset", str(path), "uvicorn"]) == 0
    assert capsys.readouterr().out == entry("uvicorn==0.54.0", "cd" * 32)
    assert make_lock.main(["subset", str(path), "nope"]) == 1
    assert make_lock.main(["subset", str(path)]) == 2
