"""Can this (verified) update be installed on this computer without the owner's help?

Checked after the signature, so everything here comes from a manifest the release builder signed. A
failed check never rejects the file: it becomes an offer with ``can_install=False`` and a
help reason (state 6, "This update needs help to install."), shown in Settings
only, never as a banner.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from . import semver

if TYPE_CHECKING:  # pragma: no cover
    from .package import Manifest

HelpReason = Literal["reinstall", "python", "launcher", "skipped_version", "runtime_missing", "disk_space"]

HELP_CODES: dict[str, str] = {
    "reinstall": "FT-UPD-HELP-REINSTALL",
    "python": "FT-UPD-HELP-PYTHON",
    "launcher": "FT-UPD-HELP-LAUNCHER",
    "skipped_version": "FT-UPD-HELP-SKIPPED",
    "runtime_missing": "FT-UPD-HELP-RUNTIME",
    "disk_space": "FT-UPD-HELP-SPACE",
}

# Room for the extracted version, the pre-update backup and the launcher's snapshot.
DISK_FACTOR = 3
DB_NAME = "fintrack.db"
DEFAULT_BOOTSTRAP_VERSION = 1  # what an install.ps1 without the field shipped


@dataclass(frozen=True)
class Help:
    reason: HelpReason
    code: str

    def as_dict(self) -> dict:
        return {"reason": self.reason, "code": self.code}


def help_for(reason: HelpReason) -> Help:
    return Help(reason, HELP_CODES[reason])


def running_python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


@dataclass
class Environment:
    """What the installed copy looks like. ``None`` roots skip the checks that need them.

    ``bootstrap_version`` is the launcher's; when not given it is read from
    ``install_root/state.json`` (written by install.ps1), defaulting to 1.
    """

    install_root: Path | None = None
    data_dir: Path | None = None
    python: str = field(default_factory=running_python)
    bootstrap_version: int | None = None
    disk_usage: Callable[[str], object] = shutil.disk_usage

    def launcher_bootstrap_version(self) -> int:
        if self.bootstrap_version is not None:
            return self.bootstrap_version
        if self.install_root is None:
            return DEFAULT_BOOTSTRAP_VERSION
        try:
            raw = json.loads((self.install_root / "state.json").read_text(encoding="utf-8"))
            value = raw.get("bootstrap_version") if isinstance(raw, dict) else None
        except (OSError, ValueError):
            value = None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        return DEFAULT_BOOTSTRAP_VERSION


def runtime_dir(install_root: Path, deps_id: str) -> Path:
    return install_root / "runtimes" / deps_id / "site-packages"


def needed_bytes(manifest: Manifest, data_dir: Path | None) -> int:
    db_size = 0
    if data_dir is not None:
        try:
            db_size = (data_dir / DB_NAME).stat().st_size
        except OSError:
            db_size = 0
    return DISK_FACTOR * manifest.payload.unpacked_size + db_size


def check(manifest: Manifest, current_version: str, env: Environment | None) -> Help | None:
    """The first reason this update needs help, or None when the user can install it themselves.

    With ``env=None`` only the checks that need nothing but the manifest run (reinstall,
    Python, skipped release); the install step must always pass a full Environment.
    """
    if manifest.requires_reinstall:
        return help_for("reinstall")
    python = env.python if env is not None else running_python()
    if manifest.python != python:
        return help_for("python")
    if env is not None and manifest.bootstrap_version > env.launcher_bootstrap_version():
        return help_for("launcher")
    if semver.compare(manifest.min_current_version, current_version) > 0:
        return help_for("skipped_version")
    if env is None or env.install_root is None:
        return None
    if manifest.kind == "app" and not runtime_dir(env.install_root, manifest.deps_id).is_dir():
        return help_for("runtime_missing")
    try:
        free = env.disk_usage(str(env.install_root)).free  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return help_for("disk_space")
    if free < needed_bytes(manifest, env.data_dir):
        return help_for("disk_space")
    return None
