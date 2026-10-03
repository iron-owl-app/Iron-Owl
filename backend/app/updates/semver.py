"""Strict MAJOR.MINOR.PATCH versions (no pre-release or build tags, no leading zeros)."""
from __future__ import annotations

import re

# ASCII digits only (``\d`` would also match other scripts' digits); at most 3 per part.
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$")

Version = tuple[int, int, int]


def is_valid(text: object) -> bool:
    return isinstance(text, str) and SEMVER_RE.fullmatch(text) is not None


def parse(text: object) -> Version:
    """``"1.4.0"`` -> ``(1, 4, 0)``; raises ValueError for anything else."""
    if not isinstance(text, str):
        raise ValueError("version must be a string")
    m = SEMVER_RE.fullmatch(text)
    if m is None:
        raise ValueError("not a MAJOR.MINOR.PATCH version")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def compare(a: str, b: str) -> int:
    """-1, 0 or 1 like the old ``cmp``."""
    pa, pb = parse(a), parse(b)
    return (pa > pb) - (pa < pb)


def display(text: str) -> str:
    """What people see: ``1.4.0`` -> ``1.4``, ``1.4.2`` -> ``1.4.2``."""
    major, minor, patch = parse(text)
    return f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"
