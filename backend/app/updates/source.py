"""Where this installed copy gets its updates: ``file``, ``github`` or ``off``.

- ``file``: signed ``.ftupdate`` files sent by email, found in Downloads or picked in
  Settings (the owner's and family installs; nothing goes online).
- ``github``: Iron Owl releases on GitHub. The app asks GitHub's API for the latest release
  of ``update_repo``, downloads its ``.ftupdate`` and verifies it (``github.py``).
- ``off``: no updates at all (git and dev copies, copies not installed by the launcher, or
  turned off in the settings file).

The build decides: each version's ``release.json`` may carry ``update_source`` ("file" or
"github") and, for github, ``update_repo`` ("owner/name", from the build, never written in
this code). Missing means "file" (every install made before 2.0.0). ``UPDATE_SOURCE`` in the
settings file (file, github or off) overrides it; an unknown value turns updates off. The
github source needs a valid ``update_repo`` from release.json, else it is off too.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

log = logging.getLogger("fintrack.updates")

UpdateSource = Literal["file", "github", "off"]
SOURCES: frozenset[str] = frozenset({"file", "github", "off"})
MAX_RELEASE_JSON_BYTES = 4096

# GitHub's rules: an owner (user or org) is 1-39 letters, digits or single hyphens, not at
# either end; a repo name is 1-100 letters, digits, ".", "_" or "-", never "." or "..".
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def valid_repo(value: object) -> bool:
    """``owner/name`` exactly as GitHub allows it (so it is safe inside an API path)."""
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    return (
        _OWNER_RE.fullmatch(owner) is not None
        and _NAME_RE.fullmatch(name) is not None
        and name not in (".", "..")
        and not name.lower().endswith(".git")
    )


def read_release(version_dir: Path) -> dict | None:
    """The running version's release.json (small, strict JSON object), or None."""
    try:
        with open(Path(version_dir) / "release.json", "rb") as fh:
            raw = fh.read(MAX_RELEASE_JSON_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_RELEASE_JSON_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _override(value: object) -> str | None:
    """``UPDATE_SOURCE`` from the settings file: None when unset, else a known source
    (anything else is "off": an unexpected value never turns on network access)."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in SOURCES:
        log.warning("UPDATE_SOURCE in the settings file is not file, github or off: updates are off")
        return "off"
    return text


def decide(
    *,
    mode: str,
    release: dict | None,
    override: object = None,
) -> tuple[UpdateSource, str | None]:
    """(source, repo). Only an installed copy (mode ``enabled``) can update at all."""
    if mode != "enabled":
        return "off", None
    built = release.get("update_source", "file") if isinstance(release, dict) else "file"
    repo = release.get("update_repo") if isinstance(release, dict) else None
    chosen = _override(override)
    if chosen is None:
        if built not in ("file", "github"):
            log.warning("release.json names an unknown update source: updates are off")
            return "off", None
        chosen = built
    if chosen == "github":
        if not valid_repo(repo):
            log.warning("GitHub updates need the repo from the build (release.json): updates are off")
            return "off", None
        return "github", repo
    return chosen, None  # type: ignore[return-value]


def source_for_settings(settings: object, mode: str, *, version_dir: Path | None = None) -> tuple[UpdateSource, str | None]:
    """``decide`` for the running app: release.json next to this version's ``backend``
    folder (``versions/<v>/release.json``) and ``settings.update_source``."""
    if mode != "enabled":
        return "off", None
    if version_dir is None:
        from . import mode as mode_module  # looked up at call time (tests move BACKEND_DIR)

        version_dir = Path(mode_module.BACKEND_DIR).parent
    return decide(mode=mode, release=read_release(version_dir), override=getattr(settings, "update_source", None))
