"""Integration tests for the versioned persistence layer as wired into the
main window: save_project(), load_project_from_path(), and autosave.

These complement tests/test_project_document.py (which covers the Qt-free
document layer directly) by proving the controller actually goes through it.
"""

import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog, QMessageBox

from _dialog import ProjectRecoveryDialog, ProjectsHomeDialog
from _focusGUI import HOI4FocusTreeGenerator
from _project_document import (
    SCHEMA_VERSION,
    load_document,
    new_document,
    save_document,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "projects"


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    monkeypatch.setattr(ProjectsHomeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    yield


@pytest.fixture
def window(qtbot):
    w = HOI4FocusTreeGenerator()
    qtbot.addWidget(w)
    yield w
    w.close()


def test_save_project_writes_current_schema(window, tmp_path):
    window.load_project_from_dict(
        json.loads((FIXTURES_DIR / "prerequisites.json").read_text(encoding="utf-8")))
    target = tmp_path / "saved.json"
    window.current_project_path = str(target)
    window.save_project()

    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert on_disk["app_version"]
    assert on_disk["metadata"]["modified_utc"]
    assert {f["id"] for f in on_disk["focus_tree"]["focuses"]} == {"a", "b", "c", "d"}


def test_legacy_file_loads_and_upgrades_on_save(window, tmp_path):
    """Acceptance: existing project files remain loadable, then get upgraded."""
    legacy = tmp_path / "legacy.json"
    legacy.write_text((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8"),
                      encoding="utf-8")

    window.load_project_from_path(str(legacy))
    assert window.tree_id == "basic_focus_tree"
    assert len(window.focuses) == 3

    window.save_project()
    upgraded = json.loads(legacy.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == SCHEMA_VERSION
    assert len(upgraded["focus_tree"]["focuses"]) == 3
    # The pre-upgrade file is retained as a backup.
    assert (tmp_path / "legacy.json.bak1").exists()


def test_round_trip_through_save_and_load(window, tmp_path):
    window.load_project_from_dict(
        json.loads((FIXTURES_DIR / "prerequisites.json").read_text(encoding="utf-8")))
    target = tmp_path / "rt.json"
    window.current_project_path = str(target)
    window.save_project()

    window2 = HOI4FocusTreeGenerator()
    try:
        window2.load_project_from_path(str(target))
        assert window2.tree_id == "prereq_focus_tree"
        assert {f.id for f in window2.focuses} == {"a", "b", "c", "d"}
        focus_c = next(f for f in window2.focuses if f.id == "c")
        assert set(focus_c.prerequisites) == {"a", "b"}
        focus_d = next(f for f in window2.focuses if f.id == "d")
        assert focus_d.prerequisites_grouped is True
        assert len(focus_d.prerequisites_groups) == 2
    finally:
        window2.close()


def test_corrupt_file_does_not_disturb_the_open_project(window, tmp_path, monkeypatch):
    """Acceptance: invalid documents do not partially alter the current canvas."""
    window.load_project_from_dict(
        json.loads((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8")))
    before_ids = {f.id for f in window.focuses}
    before_tree_id = window.tree_id

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text('{"version": "1.0.9", "tree_id": "evil", "focuses": [{"id": 1',
                       encoding="utf-8")
    # No recovery candidates exist, and the user dismisses the dialog anyway.
    monkeypatch.setattr(ProjectRecoveryDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)

    window.load_project_from_path(str(corrupt))

    assert window.tree_id == before_tree_id
    assert {f.id for f in window.focuses} == before_ids
    assert window.current_project_path != str(corrupt)


def test_invalid_field_does_not_disturb_the_open_project(window, tmp_path, monkeypatch):
    """Same guarantee for a file that parses as JSON but fails validation."""
    window.load_project_from_dict(
        json.loads((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8")))
    before_ids = {f.id for f in window.focuses}

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "1.0.9", "tree_id": "evil",
                               "focuses": [{"id": "ok"}, {"id": None}]}),
                   encoding="utf-8")
    monkeypatch.setattr(ProjectRecoveryDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)

    window.load_project_from_path(str(bad))
    assert {f.id for f in window.focuses} == before_ids


def test_recovery_dialog_offers_valid_autosave(window, tmp_path, monkeypatch):
    project = tmp_path / "p.json"
    project.write_text("{ truncated", encoding="utf-8")
    autosaves = tmp_path / "autosaves"
    autosaves.mkdir()
    save_document(str(autosaves / "p.json"), new_document("rescued", "GER"),
                  backups=False)

    # Accept whatever the dialog pre-selected (the newest valid candidate).
    def _accept(self):
        self._on_accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ProjectRecoveryDialog, "exec", _accept)

    window.load_project_from_path(str(project))
    assert window.tree_id == "rescued"
    # Recovered content adopts the original path, so the next save goes there.
    assert window.current_project_path == str(project)


def test_autosave_writes_a_loadable_document(window, tmp_path):
    window.load_project_from_dict(
        json.loads((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8")))
    window.current_project_path = str(tmp_path / "p.json")
    window.autosave_enabled = True
    window.autosave_rotate = False

    window._do_autosave()

    autosaved = tmp_path / "autosaves" / "p.json"
    assert autosaved.exists()
    doc = load_document(str(autosaved))
    assert len(doc.focus_tree["focuses"]) == 3


def test_autosave_rotation_keeps_generations(window, tmp_path):
    window.load_project_from_dict(
        json.loads((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8")))
    window.current_project_path = str(tmp_path / "p.json")
    window.autosave_enabled = True
    window.autosave_rotate = True
    window.autosave_rotate_count = 3

    for index in range(4):
        window.tree_id = f"gen{index}"
        window._do_autosave()

    autosave_dir = tmp_path / "autosaves"
    assert load_document(str(autosave_dir / "p.json")).focus_tree["tree_id"] == "gen3"
    assert load_document(str(autosave_dir / "p.json.1")).focus_tree["tree_id"] == "gen2"
    assert not (autosave_dir / "p.json.3").exists()
