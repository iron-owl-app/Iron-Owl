"""docs/MAP.md (the repo map) must match the code: regenerate it with tools/repo_map.py.

Runs the generator in-process (stdlib only, no network, no node): about two seconds.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_generator():
    spec = importlib.util.spec_from_file_location("repo_map", REPO / "tools" / "repo_map.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("repo_map", module)  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def test_repo_map_is_up_to_date() -> None:
    generator = _load_generator()
    ok, detail = generator.check(generator.generate())
    # detail starts with "docs/MAP.md is out of date: run python tools/repo_map.py".
    assert ok, detail
