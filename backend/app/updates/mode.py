"""Is the updater on for this copy of FinTrack?

- ``git``: the code folder is a git checkout (the owner's own copy). Updated with git pull;
  never scans Downloads, never creates data/updates/.
- ``not_installed``: no install root, no install-root ``state.json``, or this code isn't
  running from ``<install_root>/versions/<v>/`` (e.g. copied by hand).
- ``dev``: settings.dev (Vite dev server).
- ``enabled``: an installed copy started by the launcher.

Roots are injectable so tests never depend on where the test suite runs from.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from ..config import BACKEND_DIR, REPO_ROOT, SUPPORT_CONTACT_MAX

UpdaterMode = Literal["enabled", "git", "dev", "not_installed"]
MAX_CONTACT_CHARS = SUPPORT_CONTACT_MAX  # the same limit the settings apply


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def updater_mode(
    *,
    repo_root: Path,
    backend_dir: Path,
    install_root: Path | None,
    dev: bool,
) -> UpdaterMode:
    # ``.git`` is a folder in a normal clone and a file in a worktree: either means git.
    if (repo_root / ".git").exists():
        return "git"
    if install_root is None or not (install_root / "state.json").is_file():
        return "not_installed"
    versions = install_root / "versions"
    if not _is_under(backend_dir, versions) or backend_dir.resolve() == versions.resolve():
        return "not_installed"
    if dev:
        return "dev"
    return "enabled"


def mode_for_settings(
    settings: object,
    *,
    repo_root: Path | None = None,
    backend_dir: Path | None = None,
    install_root: Path | None = None,
) -> UpdaterMode:
    """``updater_mode`` from app settings: ``install_root`` falls back to
    ``settings.fintrack_install_root`` (FINTRACK_INSTALL_ROOT, set by the launcher) and
    ``repo_root`` to ``settings.repo_root`` (default: the folder this code runs from)."""
    root = install_root if install_root is not None else getattr(settings, "fintrack_install_root", None)
    if repo_root is None:
        configured = getattr(settings, "repo_root", None)
        repo_root = Path(configured) if configured else REPO_ROOT
    return updater_mode(
        repo_root=repo_root,
        backend_dir=backend_dir if backend_dir is not None else BACKEND_DIR,
        install_root=Path(root) if root else None,
        dev=bool(getattr(settings, "dev", False)),
    )


def support_contact(value: object) -> str:
    """The name shown in "Ask {contact}". Empty means the frontend's generic fallback
    ("the person who set up Iron Owl")."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > MAX_CONTACT_CHARS or not text.isprintable():
        return ""
    return text
