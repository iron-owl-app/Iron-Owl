"""Who to call for help: data\\support_contact.json (Settings, installer) wins over state.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAUNCHER_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import launcher_core as core  # noqa: E402
from test_launcher_core import NOW, FakeSystem, make_version, write_state  # noqa: E402


@pytest.fixture
def paths(tmp_path) -> core.Paths:
    return core.Paths(install_root=tmp_path / "Programs" / "FinTrack", home=tmp_path / "FinTrack")


def save(paths: core.Paths, raw: bytes) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    (paths.data / core.SUPPORT_CONTACT_FILE).write_bytes(raw)


def test_no_file_means_none(paths):
    assert core.read_saved_contact(paths) is None


@pytest.mark.parametrize("raw,expected", [
    (json.dumps({"support_contact": "Sam 555-0100"}).encode(), "Sam 555-0100"),
    (b"\xef\xbb\xbf" + json.dumps({"support_contact": "  Zoë "}).encode(), "Zoë"),  # BOM from PowerShell
    (json.dumps({"support_contact": ""}).encode(), ""),  # no one named, on purpose
    (json.dumps({"support_contact": "Sa\u0000m\n" + "x" * 100}).encode(), "Sam" + "x" * 57),
])
def test_saved_names_are_cleaned(paths, raw, expected):
    save(paths, raw)
    assert core.read_saved_contact(paths) == expected


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]", b'{"support_contact": 1}', b'{"x": "y"}',
                                 b'{"support_contact": "' + b"a" * 5000 + b'"}'])
def test_unusable_files_are_ignored(paths, raw):
    save(paths, raw)
    assert core.read_saved_contact(paths) is None


def _failing_launch(paths: core.Paths) -> FakeSystem:
    make_version(paths, "1.4.0")
    write_state(paths, support_contact="From state")
    core.write_control(paths.control_file, 8000, "c" * 43, 1, NOW)
    system = FakeSystem()
    system.external[8000] = ("c" * 43, core.Health("ok", version="1.4.0", boot_id="ab" * 8, web=False))
    lau = core.Launcher(paths, system, core.Options())
    assert lau.launch() == 1
    return system


def test_message_box_uses_the_saved_name(paths):
    save(paths, json.dumps({"support_contact": "Sam"}).encode())
    assert _failing_launch(paths).errors_shown == [core.error_message("Sam")]


def test_message_box_falls_back_to_state_json(paths):
    assert _failing_launch(paths).errors_shown == [core.error_message("From state")]


def test_blank_saved_name_beats_state_json(paths):
    save(paths, json.dumps({"support_contact": ""}).encode())
    assert _failing_launch(paths).errors_shown == [core.error_message("")]
