"""Iron Owl 2.0.0 installer and GitHub build: static checks (Inno Setup isn't needed to run them).

The .iss is checked as text: it references files that exist, takes its version from the build
(never hard-coded), hands the support contact over in a file, and never registers an
uninstaller of its own. The version lives in one place (backend/app/version.py) and every
other copy must match it. The workflows only run in the public repo, with minimal permissions.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.config import REPO_ROOT, SUPPORT_CONTACT_MAX
from app.version import __version__

INSTALLER = REPO_ROOT / "installer"
ISS = INSTALLER / "iron-owl.iss"
INSTALL_PS1 = REPO_ROOT / "packaging" / "install.ps1"
UNINSTALL_PS1 = REPO_ROOT / "packaging" / "uninstall.ps1"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLIC_REPO = "iron-owl-app/Iron-Owl"


def iss_text() -> str:
    return ISS.read_text(encoding="utf-8")


def iss_section(name: str) -> list[str]:
    lines, inside = [], False
    for line in iss_text().splitlines():
        if line.startswith("["):
            inside = line.strip() == f"[{name}]"
            continue
        if inside and line.strip() and not line.lstrip().startswith(";"):
            lines.append(line)
    return lines


def setup_values() -> dict[str, str]:
    values = {}
    for line in iss_section("Setup"):
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def pascal_function(name: str) -> str:
    text = iss_text()
    start = re.search(rf"^(?:function|procedure) {name}\b.*$", text, re.MULTILINE)
    assert start, name
    end = re.search(r"^end;$", text[start.end():], re.MULTILINE)
    return text[start.start():start.end() + end.end()]


# ------------------------------------------------------------------ version: one place


def test_version_is_2_0_0_everywhere():
    assert __version__ == "2.0.0"
    pkg = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((REPO_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert pkg["version"] == lock["version"] == lock["packages"][""]["version"] == __version__


def test_installer_version_comes_from_version_py():
    text = iss_text()
    assert "#ifndef AppVersion" in text and "#error AppVersion is not defined" in text
    values = setup_values()
    assert values["AppVersion"] == "{#AppVersion}"
    assert values["OutputBaseFilename"] == "Iron-Owl-Setup-{#AppVersion}"
    # no version number typed into the script (MinVersion is Windows 10)
    assert not re.search(r"\b\d+\.\d+\.\d+\b", "\n".join(iss_section("Setup")))
    build = (INSTALLER / "build_installer.ps1").read_text(encoding="utf-8")
    assert r"backend\app\version.py" in build and '"/DAppVersion=$version"' in build


def test_release_notes_start_fresh_at_this_version():
    notes = (REPO_ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## (\S+)$", notes, re.MULTILINE)
    assert sections == [__version__]
    sys.path.insert(0, str(REPO_ROOT / "tools" / "release"))
    import build_update as bu

    bullets, _ = bu.read_release_notes(REPO_ROOT, __version__)  # the release build's own rules
    assert 2 <= len(bullets) <= 4
    assert "FinTrack" not in notes  # strangers never knew the old name


# ------------------------------------------------------------------ the .iss


def test_iss_is_ascii_and_references_existing_files():
    raw = ISS.read_bytes()
    raw.decode("ascii")  # no encoding surprises for ISCC
    values = setup_values()
    for key in ("SetupIconFile", "WizardImageFile", "WizardSmallImageFile"):
        path = (INSTALLER / values[key].replace("\\", "/")).resolve()
        assert path.is_file(), (key, path)
        assert REPO_ROOT.resolve() in path.parents
    assert (INSTALLER / "build_installer.ps1").is_file()
    bmp = (INSTALLER / values["WizardImageFile"].replace("\\", "/")).read_bytes()
    assert bmp[:2] == b"BM"
    ico = (INSTALLER / values["SetupIconFile"].replace("\\", "/")).read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00"


def test_iss_installs_per_user_and_leaves_uninstall_to_install_ps1():
    values = setup_values()
    assert values["PrivilegesRequired"] == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in values
    assert values["Uninstallable"] == "no" and values["CreateUninstallRegKey"] == "no"
    assert values["CreateAppDir"] == "no"
    assert values["AppName"] == "{#AppName}" and '#define AppName "Iron Owl"' in iss_text()
    assert values["AppId"].startswith("{{") and "SignTool" not in values  # unsigned for 2.0.0
    files = iss_section("Files")
    assert files == ['Source: "{#PackageDir}\\*"; DestDir: "{tmp}\\package"; Flags: ignoreversion recursesubdirs createallsubdirs']


def test_iss_runs_install_ps1_unattended_with_the_contact_in_a_file():
    params = pascal_function("InstallParams")
    for flag in ("-Unattended", "-LogFile", "-ResultFile", "-SupportContactFile", "-NoDesktopShortcut",
                 "-NoProfile", "-NonInteractive", "-ExecutionPolicy Bypass", "install.ps1"):
        assert flag in params, flag
    # the typed name never goes on the command line: only fixed file paths do
    assert "ContactName" not in params and "ContactPage" not in params and "Values[" not in params
    write = pascal_function("WriteContactFile")
    assert "SaveStringsToUTF8File(ContactFile()" in write and "ContactName()" in write
    run = "\n".join(iss_section("Run"))
    assert "{code:InstallParams}" in run and "runhidden" in run and "AfterInstall: CheckInstallResult" in run
    assert "Check: InstallWorked" in run
    assert f"#define ContactMax {SUPPORT_CONTACT_MAX}" in iss_text()
    assert "Edits[0].MaxLength := {#ContactMax}" in iss_text()


def test_install_ps1_takes_what_the_installer_passes():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    param_block = text[text.index("param("):text.index(")\n$ErrorActionPreference")]
    for name in ("SupportContactFile", "Unattended", "LogFile", "ResultFile", "NoDesktopShortcut", "SupportContact"):
        assert f"${name}" in param_block, name
    assert f"$contactMax = {SUPPORT_CONTACT_MAX}" in text
    assert 'DisplayName     = $appName' in text and '$appName = "Iron Owl"' in text
    assert "Read-Host" in text and "-not $Unattended" in text  # the owner's hand install still asks
    assert "support_contact.json" in text


def test_install_log_never_holds_the_contact():
    """M1: the -LogFile transcript (kept in %TEMP%) must not hold the name and phone."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    echoes = [line for line in text.splitlines()
              if re.search(r"Write-(Host|Output)[^\n]*\$SupportContact", line)]
    assert echoes == ['elseif ($SupportContact) { Write-Host "Messages will say: call $SupportContact." }']
    assert 'if ($SupportContact -and $LogFile) { Write-Host "Messages will say who to call (the name you gave)." }' in text


def test_uninstall_keeps_data_and_removes_both_shortcut_names():
    text = UNINSTALL_PS1.read_text(encoding="utf-8")
    assert '@("Iron Owl.lnk", "FinTrack.lnk")' in text
    assert "Your data will be kept" in text and "[switch]$RemoveData" in text


def test_uninstall_asks_with_a_windows_box_not_a_typed_answer():
    text = UNINSTALL_PS1.read_text(encoding="utf-8")
    # The normal path (no -RemoveData, no -Yes) uses Yes/No boxes that say the data is kept.
    assert "$useBoxes = -not $Yes -and -not $RemoveData" in text
    ask = text[text.index("if ($useBoxes) {"):text.index("} elseif ($RemoveData -and -not $Yes) {")]
    assert '"YesNo"' in ask and "Your data will be kept" in ask and "Read-Host" not in ask
    assert 'if ($answer -ne "Yes") { exit 0 }' in ask
    # Every Read-Host is behind -RemoveData (a PowerShell window only).
    lines = text.splitlines()
    asks = [i for i, line in enumerate(lines) if "Read-Host" in line]
    assert len(asks) == 2
    for i in asks:
        block_start = next(lines[j] for j in range(i, -1, -1) if lines[j] and not lines[j][0].isspace())
        assert "$RemoveData -and -not $Yes) {" in block_start, lines[i]
    assert "Your data is still on this PC" in text  # the "done" box
    assert "Show-Box \"Iron Owl couldn't be fully removed." in text  # failures show a box too
    # Settings > Apps runs it without a console window.
    install = INSTALL_PS1.read_text(encoding="utf-8")
    assert 'UninstallString = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File' in install


def test_help_says_click_yes_to_uninstall():
    help_text = (REPO_ROOT / "HELP.md").read_text(encoding="utf-8")
    assert "click **Yes**" in help_text and "type **y**" not in help_text


def test_package_ships_the_notices_and_the_new_icon():
    build = (REPO_ROOT / "tools" / "release" / "build_package.ps1").read_text(encoding="utf-8")
    assert r'"packaging\brand\iron-owl.ico"' in build and '"THIRD_PARTY_NOTICES.md"' in build
    assert '"Iron-Owl-$version"' in build
    notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for line in (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name, version = re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(\S+)", line).groups()
        assert re.search(rf"^\| {re.escape(name)} \| {re.escape(version)} \|", notices, re.MULTILINE | re.IGNORECASE), line
    deps = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["dependencies"]
    for name in deps:
        assert name.replace("@fontsource/ibm-plex-sans", "IBM Plex Sans") in notices, name
    assert "Python Software Foundation" in notices and "SIL Open Font License" in notices
    assert (REPO_ROOT / "frontend" / "public" / "licenses" / "IBM-Plex-Sans-OFL.txt").is_file()


def test_package_installs_the_lock_with_make_lock():
    """plaid-python publishes only an sdist: with the lock, the runtime comes from make_lock.py
    install-runtime (it builds that sdist with the pinned backend first), never a plain
    wheels-only pip install of the lock, and a stale build-lock.txt stops the build early."""
    build = (REPO_ROOT / "tools" / "release" / "build_package.ps1").read_text(encoding="utf-8")
    assert '@($makeLock, "install-runtime", $lockFile, $site, "--python-version", "3.11")' in build
    assert '$lockArgs += @("--wheel-dir", $WheelDir)' in build
    assert '"-r", $lockFile' not in build
    assert r'"tools\release\build-lock.txt"' in build and "lock-id $buildLock" in build
    wf = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "tools/release/build-lock.txt" in wf
    for name in ("requirements-lock.txt", "build-lock.txt"):
        assert (REPO_ROOT / "tools" / "release" / name).is_file(), name


def test_contact_limit_is_the_same_everywhere():
    launcher = (REPO_ROOT / "packaging" / "launcher" / "launcher_core.py").read_text(encoding="utf-8")
    assert f"SUPPORT_CONTACT_MAX = {SUPPORT_CONTACT_MAX}" in launcher
    api = (REPO_ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    assert f"export const SUPPORT_CONTACT_MAX = {SUPPORT_CONTACT_MAX};" in api


# ------------------------------------------------------------------ PowerShell (Windows only)


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL, reason="needs Windows PowerShell")


def run_ps(script: str, tmp_path: Path) -> str:
    path = tmp_path / "t.ps1"
    path.write_text(script, encoding="utf-8-sig")
    done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


@needs_powershell
@pytest.mark.parametrize("rel", ["packaging/install.ps1", "packaging/uninstall.ps1", "tools/release/build_package.ps1",
                                 "installer/build_installer.ps1", "create-shortcut.ps1"])
def test_powershell_scripts_parse(tmp_path, rel):
    script = (
        "$errs = $null; $tokens = $null\n"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{REPO_ROOT / rel}', [ref]$tokens, [ref]$errs) | Out-Null\n"
        "if ($errs.Count) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 }\n"
        "Write-Output 'ok'\n"
    )
    assert run_ps(script, tmp_path).strip() == "ok"


def _ps_function(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    depth, i = 0, text.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
        if depth == 0:
            return text[start:i]


@needs_powershell
def test_uninstall_box_arguments_are_real_windows_names(tmp_path):
    """Show-Box turns its strings into WinForms enums; a typo would only fail on a real uninstall."""
    text = UNINSTALL_PS1.read_text(encoding="utf-8")
    calls = re.findall(r'"(OK|YesNo)" "(\w+)"', text)
    assert ("YesNo", "Question") in calls and ("OK", "Warning") in calls
    lines = ["$ErrorActionPreference = 'Stop'", "Add-Type -AssemblyName System.Windows.Forms"]
    for buttons, icon in calls + [("OK", "Information")]:
        lines.append(f"[void][System.Windows.Forms.MessageBoxButtons]::{buttons}; [void][System.Windows.Forms.MessageBoxIcon]::{icon}")
    lines.append("Write-Output 'ok'")
    assert run_ps("\n".join(lines), tmp_path).strip() == "ok"


@needs_powershell
def test_install_ps1_cleans_the_contact_like_the_app(tmp_path):
    text = INSTALL_PS1.read_text(encoding="utf-8")
    contact_file = tmp_path / "contact.txt"
    contact_file.write_bytes(b"\xef\xbb\xbf  Zo\xc3\xab 555-0100 \r\n")
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$contactMax = {SUPPORT_CONTACT_MAX}",
        "$contactFileMaxBytes = 4096",
        _ps_function(text, "Get-CleanContact"),
        _ps_function(text, "Read-ContactFile"),
        "$out = [ordered]@{}",
        "$out.control = Get-CleanContact ('Sa' + [char]0 + 'm' + [char]10 + [char]0x202E)",
        "$out.long = Get-CleanContact ('x' * 70)",
        "$out.emoji = Get-CleanContact (('y' * 59) + [char]::ConvertFromUtf32(0x1F989))",
        "$out.blank = Get-CleanContact '   '",
        f"$out.file = Get-CleanContact (Read-ContactFile '{contact_file}')",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "Write-Output ($out | ConvertTo-Json -Compress)",
    ])
    got = json.loads(run_ps(script, tmp_path))
    assert got == {"control": "Sam", "long": "x" * SUPPORT_CONTACT_MAX, "emoji": "y" * 59, "blank": "",
                   "file": "Zoë 555-0100"}


# ------------------------------------------------------------------ GitHub workflows


def load_workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key "on" as True
    return data


@pytest.mark.parametrize("name", ["tests.yml", "release.yml"])
def test_workflows_run_only_in_the_public_repo_with_minimal_permissions(name):
    wf = load_workflow(name)
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "pull_request_target" not in text and "workflow_run" not in text
    assert wf["permissions"] == {"contents": "read"}
    for job_name, job in wf["jobs"].items():
        assert job["if"] == f"github.repository == '{PUBLIC_REPO}'", job_name
        expected = {"contents": "write"} if job_name == "release" else {"contents": "read"}
        assert job["permissions"] == expected, job_name
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:  # official actions, pinned to a full commit SHA
                assert re.fullmatch(r"actions/(checkout|setup-python|setup-node|upload-artifact|download-artifact)@[0-9a-f]{40}",
                                    uses), uses
            if uses and uses.startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
            # secrets reach scripts only through env, never pasted into the script text
            assert "secrets." not in str(step.get("run", "")), step.get("name")


@pytest.mark.parametrize("name", ["tests.yml", "release.yml"])
def test_workflow_actions_are_pinned_with_their_tag(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    uses = re.findall(r"^\s*-?\s*uses: (\S+)(.*)$", text, re.MULTILINE)
    assert uses
    for ref, rest in uses:
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r" # v\d+\.\d+\.\d+", rest), (ref, rest)
    # one action is pinned to the same SHA everywhere
    pins: dict[str, set[str]] = {}
    for workflow in ("tests.yml", "release.yml"):
        for ref, rest in re.findall(r"uses: (\S+)(.*)", (WORKFLOWS / workflow).read_text(encoding="utf-8")):
            pins.setdefault(ref.split("@")[0], set()).add(ref + rest)
    assert all(len(v) == 1 for v in pins.values()), pins
    assert "To update one" in text and "gh api repos/actions/checkout/git/ref/tags/" in text


def test_tests_workflow_runs_every_check():
    wf = load_workflow("tests.yml")
    assert set(wf["on"]) == {"push", "pull_request"}
    job = wf["jobs"]["tests"]
    assert job["runs-on"] == "windows-latest"
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    for needed in ("pytest", "npm ci", "npx tsc --noEmit", "npm run test:unit", "python tools/repo_map.py --check",
                   "requirements-dev.txt"):
        assert needed in runs, needed
    pythons = [s["with"]["python-version"] for s in job["steps"] if s.get("uses", "").startswith("actions/setup-python")]
    assert pythons == ["3.11"]


def _secret_env(step: dict) -> list[str]:
    return sorted(k for k, v in (step.get("env") or {}).items() if "secrets." in str(v))


def test_release_workflow_builds_without_secrets_and_signs_in_its_own_job():
    """S1: the signing key and passphrase never share a machine with tests, npm or the web
    build; the signing job runs only the repo's own code from a clean checkout of the tag."""
    wf = load_workflow("release.yml")
    assert wf["on"] == {"push": {"tags": ["v*"]}}
    jobs = wf["jobs"]
    assert list(jobs) == ["build", "sign", "release"]
    build, sign, release = jobs["build"], jobs["sign"], jobs["release"]

    # build: no secrets anywhere, the unsigned update only
    assert "secrets." not in json.dumps(build)
    steps = {s.get("name", s.get("uses")): s for s in build["steps"]}
    update = steps["Build the unsigned update file"]["run"]
    for needed in ("--source github", "--update-repo $env:GITHUB_REPOSITORY", "--tag $env:TAG", "--unsigned-out"):
        assert needed in update, needed
    assert "--passphrase" not in update and "--key" not in update
    package = steps["Build the Windows package"]["run"]
    assert "-UpdateSource github" in package and "-RequireLock" in package
    assert "build_installer.ps1" in steps["Build the setup .exe"]["run"]
    uploads = [s["with"]["name"] for s in build["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert uploads == ["unsigned-update", "installer"]

    # sign: its own job and environment; secrets reach exactly one step
    assert sign["needs"] == "build" and sign["environment"] == "release"
    assert sign["permissions"] == {"contents": "read"}
    sign_steps = sign["steps"]
    names = [s.get("name", s.get("uses")) for s in sign_steps]
    with_secrets = [n for n, s in zip(names, sign_steps) if _secret_env(s)]
    assert with_secrets == ["Sign the update file"]
    step = sign_steps[names.index("Sign the update file")]
    assert _secret_env(step) == ["IRON_OWL_SIGNING_KEY", "IRON_OWL_SIGNING_PASSPHRASE"]
    run = step["run"]
    # the secrets leave the environment before python starts; the passphrase goes on stdin
    remove_at = run.index("Remove-Item -Path Env:IRON_OWL_SIGNING_KEY, Env:IRON_OWL_SIGNING_PASSPHRASE")
    python_at = run.index("| python tools\\release\\build_update.py --sign-only")
    assert remove_at < run.index("WriteAllText($pem") < python_at
    for needed in ("--passphrase-stdin", "--source github", "--tag $env:TAG", "icacls", "::add-mask::"):
        assert needed in run, needed
    assert "--passphrase " not in run
    # the key folder is deleted in a finally, right after python, and again in a backstop step
    assert run.index("} finally {") > python_at and "Remove-Item -LiteralPath $dir -Recurse -Force" in run.split("} finally {")[1]
    backstop = sign_steps[names.index("Delete the signing key (backstop)")]
    assert backstop["if"] == "always()" and "signing" in backstop["run"]
    # nothing in the signing job builds, tests or installs from npm; pip only installs the
    # hash-checked cryptography wheels, before any secret exists
    runs = "\n".join(str(s.get("run", "")) for s in sign_steps)
    for banned in ("npm", "pytest", "build_package", "build_installer", "requirements-dev", "--unsigned-out"):
        assert banned not in runs, banned
    install = sign_steps[names.index("Install the signing library")]["run"]
    assert "--require-hashes" in install and "--no-deps" in install and "--only-binary=:all:" in install
    assert "make_lock.py subset tools\\release\\requirements-lock.txt cryptography cffi pycparser" in install
    assert names.index("Install the signing library") < names.index("Sign the update file")
    assert not any(str(s.get("uses", "")).startswith(("actions/setup-node", "actions/cache")) for s in sign_steps)
    assert all(not s.get("with", {}).get("cache") for s in sign_steps)

    # release: no repository code, just the two artifacts and the draft
    assert release["needs"] == ["build", "sign"]
    assert not any(str(s.get("uses", "")).startswith("actions/checkout") for s in release["steps"])
    gh = release["steps"][-1]["run"]
    assert "gh release create" in gh and "--draft" in gh and "sha256sum" in gh
    assert 'Iron-Owl-$VERSION.ftupdate' in gh and 'Iron-Owl-Setup-$VERSION.exe' in gh
    assert release["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    # the old one-job signing comment is gone
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "gone before any other build step runs" not in text
    embed = json.loads((REPO_ROOT / "tools" / "release" / "python-embed.json").read_text(encoding="utf-8"))
    assert embed["url"].startswith("https://www.python.org/ftp/python/3.11.")
    assert embed["sha256"] == "" or re.fullmatch(r"[0-9a-f]{64}", embed["sha256"])


@needs_powershell
@pytest.mark.parametrize("name", ["tests.yml", "release.yml"])
def test_workflow_powershell_steps_parse(tmp_path, name):
    """Every pwsh run block parses (a typo would only show on a real tag push)."""
    wf = load_workflow(name)
    blocks = []
    for job in wf["jobs"].values():
        shell = (job.get("defaults") or {}).get("run", {}).get("shell")
        for step in job.get("steps", []):
            if "run" in step and step.get("shell", shell) == "pwsh":
                blocks.append(step["run"])
    assert blocks
    lines = ["$failed = 0"]
    for i, block in enumerate(blocks):
        path = tmp_path / f"step{i}.ps1"
        path.write_text(block, encoding="utf-8-sig")
        lines += [
            "$errs = $null; $tokens = $null",
            f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs) | Out-Null",
            f"if ($errs.Count) {{ $errs | ForEach-Object {{ Write-Output ('step{i}: ' + $_.Message) }}; $failed = 1 }}",
        ]
    lines.append("if ($failed) { exit 1 }; Write-Output 'ok'")
    assert run_ps("\n".join(lines), tmp_path).strip() == "ok"
