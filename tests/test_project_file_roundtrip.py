"""End-to-end project save/load smoke test that goes through disk, using
the real atomic_write_json() writer and load_project_from_path() reader
(source/_focusGUI.py), rather than just the in-memory dict helpers
covered by test_project_roundtrip.py.

load_project_from_path() calls QMessageBox.information(...) on success,
which -- like the Projects Home dialog -- is a real modal exec() call
that hangs forever headless. We patch QMessageBox.information to a
no-op for the duration of this test only; production behavior is
untouched.
"""

import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog, QMessageBox

import _focusGUI as focusgui_module
from _focusGUI import HOI4FocusTreeGenerator, atomic_write_json
from _dialog import ProjectsHomeDialog

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "projects"


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    monkeypatch.setattr(ProjectsHomeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    # load_project_from_path() pops a QMessageBox.information on success.
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    yield


@pytest.fixture
def window(qtbot):
    w = HOI4FocusTreeGenerator()
    qtbot.addWidget(w)
    yield w
    w.close()


def test_save_then_load_project_file_round_trips_via_disk(window, tmp_path):
    fixture_path = FIXTURES_DIR / "prerequisites.json"
    original = json.loads(fixture_path.read_text(encoding="utf-8"))

    # Load fixture into the live window, then use the app's own save path
    # (_build_project_payload + atomic_write_json, exactly what
    # save_project() does) to write it back out to a throwaway file.
    window.load_project_from_dict(original)
    payload = window._build_project_payload()
    out_file = tmp_path / "roundtrip_project.json"
    atomic_write_json(str(out_file), payload)

    assert out_file.exists()
    on_disk = json.loads(out_file.read_text(encoding="utf-8"))
    assert on_disk["tree_id"] == "prereq_focus_tree"
    assert {f["id"] for f in on_disk["focuses"]} == {"a", "b", "c", "d"}

    # Now load that file back through the real file-loading code path
    # into a second, fresh window and confirm the modeled data survives.
    window2 = HOI4FocusTreeGenerator()
    try:
        window2.load_project_from_path(str(out_file))
        assert window2.current_project_path == str(out_file)
        assert window2.tree_id == "prereq_focus_tree"
        assert {f.id for f in window2.focuses} == {"a", "b", "c", "d"}
        focus_c = next(f for f in window2.focuses if f.id == "c")
        assert set(focus_c.prerequisites) == {"a", "b"}
        focus_d = next(f for f in window2.focuses if f.id == "d")
        assert focus_d.prerequisites_grouped is True
        assert len(focus_d.prerequisites_groups) == 2
    finally:
        window2.close()
