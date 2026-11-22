"""Unit tests for the versioned project-document layer (_project_document).

These are deliberately Qt-free: the module under test must be usable and
testable without a QApplication, so nothing here imports PyQt.
"""

import json
import os
from pathlib import Path

import pytest

from _project_document import (
    DEFAULT_BACKUP_KEEP,
    SCHEMA_VERSION,
    ProjectDocument,
    ProjectDocumentError,
    ValidationIssue,
    atomic_write_text,
    best_recovery_candidate,
    detect_schema_version,
    dumps,
    find_recovery_candidates,
    load_document,
    loads,
    migrate,
    new_document,
    read_summary,
    rotate_backups,
    save_document,
    save_rotating,
    set_editor_preference,
    validate,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "projects"
ALL_FIXTURES = sorted(FIXTURES_DIR.glob("*.json"))


# --- schema / migration ---------------------------------------------------

def test_detect_schema_version_treats_legacy_files_as_v0():
    legacy = json.loads((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8"))
    assert detect_schema_version(legacy) == 0


def test_detect_schema_version_rejects_non_integer():
    with pytest.raises(ProjectDocumentError) as exc:
        detect_schema_version({"schema_version": "1"})
    assert exc.value.issues[0].path == "schema_version"


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.stem)
def test_every_repo_fixture_still_loads(fixture):
    """Acceptance: existing project files remain loadable."""
    doc = load_document(str(fixture))
    assert doc.migrated_from == 0
    assert validate(doc.to_json_obj()) == []
    original = json.loads(fixture.read_text(encoding="utf-8"))
    assert doc.focus_tree["tree_id"] == original["tree_id"]
    assert len(doc.focus_tree["focuses"]) == len(original["focuses"])


def test_migration_maps_legacy_sections():
    legacy = {
        "version": "1.0.9",
        "tree_id": "t",
        "country_tag": "GER",
        "focuses": [{"id": "a"}],
        "events": [{"id": "e"}],
        "notes": [{"id": "n"}],
        "settings": {
            "canvas": {"grid": 10},
            "preferences": {"prefer_app_settings": True},
            "keybindings": {"save": "Ctrl+S"},
        },
    }
    migrated, steps = migrate(legacy)
    assert steps == [0]
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert migrated["focus_tree"]["focuses"] == [{"id": "a"}]
    assert migrated["events"]["events"] == [{"id": "e"}]
    assert migrated["annotations"]["notes"] == [{"id": "n"}]
    assert migrated["canvas"]["settings"] == {"grid": 10}
    assert migrated["editor"]["preferences"] == {"prefer_app_settings": True}
    assert migrated["editor"]["keybindings"] == {"save": "Ctrl+S"}


def test_migration_handles_new_project_stub():
    """The minimal dict new_project() used to write is a valid v0 document."""
    doc = ProjectDocument.from_legacy(
        {"version": "1.0.9", "tree_id": "stub", "country_tag": "TAG",
         "focuses": [], "library": {}})
    assert validate(doc.to_json_obj()) == []
    assert doc.app_version == "1.0.9"


def test_migration_handles_imported_txt_marker():
    """'imported_txt_v1' is provenance, not an app version number."""
    doc = ProjectDocument.from_legacy(
        {"version": "imported_txt_v1", "tree_id": "x", "focuses": []},
        app_version="2.0.0")
    assert doc.app_version == "2.0.0"
    assert doc.metadata["legacy_version"] == "imported_txt_v1"


def test_migrate_rejects_future_schema():
    with pytest.raises(ProjectDocumentError, match="newer version"):
        migrate({"schema_version": SCHEMA_VERSION + 1})


def test_migrate_does_not_mutate_input():
    legacy = {"version": "1.0.9", "tree_id": "t", "focuses": [{"id": "a"}]}
    snapshot = json.dumps(legacy, sort_keys=True)
    migrate(legacy)
    assert json.dumps(legacy, sort_keys=True) == snapshot


# --- extensions -----------------------------------------------------------

def test_unknown_top_level_fields_survive_round_trip(tmp_path):
    """Acceptance: unknown extension data survives round trips."""
    legacy = {
        "version": "1.0.9",
        "tree_id": "t",
        "focuses": [],
        "some_future_field": {"nested": [1, 2, 3]},
        "states": {"12": {"owner": "GER"}},
    }
    path = tmp_path / "p.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    doc = load_document(str(path))
    assert doc.extensions["some_future_field"] == {"nested": [1, 2, 3]}
    assert doc.extensions["states"] == {"12": {"owner": "GER"}}

    save_document(str(path), doc)
    reloaded = load_document(str(path))
    assert reloaded.extensions["some_future_field"] == {"nested": [1, 2, 3]}
    # ...and it also reaches the editor's flat dialect.
    assert reloaded.to_legacy()["states"] == {"12": {"owner": "GER"}}


def test_recognised_sections_win_over_extensions_in_to_legacy():
    doc = ProjectDocument()
    doc.focus_tree["focuses"] = [{"id": "real"}]
    doc.extensions["focuses"] = [{"id": "stale"}]
    assert doc.to_legacy()["focuses"] == [{"id": "real"}]


# --- validation -----------------------------------------------------------

def test_validation_reports_exact_path_and_value():
    obj = ProjectDocument().to_json_obj()
    obj["focus_tree"]["focuses"] = [{"id": "ok"}, {"id": ""}, {"id": "x", "x": "NaN"}]
    issues = validate(obj)
    paths = {i.path: i.value for i in issues}
    assert paths["focus_tree.focuses[1].id"] == ""
    assert paths["focus_tree.focuses[2].x"] == "NaN"


def test_validation_flags_duplicate_focus_ids():
    obj = ProjectDocument().to_json_obj()
    obj["focus_tree"]["focuses"] = [{"id": "dup"}, {"id": "dup"}]
    issues = validate(obj)
    assert any("duplicate" in i.message for i in issues)
    assert any(i.path == "focus_tree.focuses[1].id" for i in issues)


def test_validation_flags_bad_prerequisite_element():
    obj = ProjectDocument().to_json_obj()
    obj["focus_tree"]["focuses"] = [{"id": "a", "prerequisites": ["b", 7]}]
    issues = validate(obj)
    assert any(i.path == "focus_tree.focuses[0].prerequisites[1]" for i in issues)


def test_validation_flags_bad_link_pair():
    obj = ProjectDocument().to_json_obj()
    obj["events"]["event_focus_links"] = [["a", "b"], ["only-one"]]
    issues = validate(obj)
    assert any(i.path == "events.event_focus_links[1]" for i in issues)


def test_valid_documents_produce_no_issues():
    assert validate(new_document("t", "GER").to_json_obj()) == []


def test_load_raises_with_issue_detail_on_bad_document(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": "1.0.9", "tree_id": "t",
                                "focuses": [{"id": 42}]}), encoding="utf-8")
    with pytest.raises(ProjectDocumentError) as exc:
        load_document(str(path))
    assert exc.value.issues[0].path == "focus_tree.focuses[0].id"
    assert exc.value.path == str(path)


def test_load_raises_on_truncated_json(tmp_path):
    path = tmp_path / "trunc.json"
    path.write_text('{"version": "1.0.9", "focuses": [', encoding="utf-8")
    with pytest.raises(ProjectDocumentError, match="not valid JSON"):
        load_document(str(path))


def test_load_raises_on_non_object_json(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ProjectDocumentError):
        load_document(str(path))


# --- deterministic serialization ------------------------------------------

def test_dumps_is_key_order_independent():
    a = {"schema_version": 1, "app_version": "1", "focus_tree": {"tree_id": "t", "focuses": []}}
    b = {"focus_tree": {"focuses": [], "tree_id": "t"}, "app_version": "1", "schema_version": 1}
    assert dumps(a) == dumps(b)


def test_dumps_normalises_integral_floats_and_negative_zero():
    assert dumps({"x": 3.0, "y": -0.0}) == dumps({"x": 3, "y": 0.0})


def test_dumps_rejects_non_finite_numbers():
    with pytest.raises(ProjectDocumentError) as exc:
        dumps({"focus_tree": {"focuses": [{"id": "a", "x": float("inf")}]}})
    assert "focuses[0].x" in exc.value.issues[0].path


def test_save_reload_save_is_byte_stable(tmp_path):
    """Acceptance: save, reload, and save again are semantically identical."""
    path = tmp_path / "p.json"
    doc = load_document(str(FIXTURES_DIR / "frames_and_notes.json"))
    first = save_document(str(path), doc, backups=False)
    reloaded = load_document(str(path))
    second = save_document(str(path), reloaded, backups=False)
    # created_utc is preserved by touch(); modified_utc may differ, so
    # compare everything else.
    first_obj = json.loads(first)
    second_obj = json.loads(second)
    first_obj["metadata"].pop("modified_utc")
    second_obj["metadata"].pop("modified_utc")
    assert first_obj == second_obj
    assert first_obj["metadata"]["created_utc"] == second_obj["metadata"]["created_utc"]


# --- atomic writes --------------------------------------------------------

def _files_in(directory) -> list:
    """Names of regular files, ignoring the conftest fake_home subdirectory."""
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "p.json"
    atomic_write_text(str(target), '{"a": 1}\n')
    assert _files_in(tmp_path) == ["p.json"]


def test_interrupted_write_leaves_original_intact(tmp_path, monkeypatch):
    """Acceptance: interrupted writes cannot replace a valid project."""
    path = tmp_path / "p.json"
    good = new_document("keeper", "GER")
    save_document(str(path), good, backups=False)
    before = path.read_text(encoding="utf-8")

    import _project_document as pd

    def boom(src, dst):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(pd.os, "replace", boom)
    doomed = new_document("clobberer", "SOV")
    with pytest.raises(OSError):
        save_document(str(path), doomed, backups=False)
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == before
    assert load_document(str(path)).focus_tree["tree_id"] == "keeper"
    # The aborted temp file must not be left lying around.
    assert _files_in(tmp_path) == ["p.json"]


def test_serialization_failure_never_touches_disk(tmp_path):
    path = tmp_path / "p.json"
    save_document(str(path), new_document("keeper"), backups=False)
    before = path.read_text(encoding="utf-8")

    bad = new_document("bad")
    bad.focus_tree["focuses"] = [{"id": "a", "x": float("nan")}]
    with pytest.raises(ProjectDocumentError):
        save_document(str(path), bad, backups=False)
    assert path.read_text(encoding="utf-8") == before


# --- backups and rotation -------------------------------------------------

def test_rotating_backups_respect_retention(tmp_path):
    path = tmp_path / "p.json"
    save_document(str(path), new_document("gen0"), backups=False)
    for generation in range(1, DEFAULT_BACKUP_KEEP + 4):
        save_document(str(path), new_document(f"gen{generation}"),
                      keep=DEFAULT_BACKUP_KEEP)

    backups = sorted(p.name for p in tmp_path.glob("p.json.bak*"))
    assert len(backups) == DEFAULT_BACKUP_KEEP
    # .bak1 is the immediately-previous generation, .bakN the oldest kept.
    last_written = DEFAULT_BACKUP_KEEP + 3
    for index in range(1, DEFAULT_BACKUP_KEEP + 1):
        doc = load_document(str(tmp_path / f"p.json.bak{index}"))
        assert doc.focus_tree["tree_id"] == f"gen{last_written - index}"


def test_first_save_creates_no_backup(tmp_path):
    path = tmp_path / "fresh.json"
    save_document(str(path), new_document("fresh"))
    assert list(tmp_path.glob("*.bak*")) == []


def test_autosave_rotation_keeps_generations(tmp_path):
    base = tmp_path / "auto.json"
    for generation in range(6):
        save_rotating(str(base), new_document(f"gen{generation}"), keep=3)
    assert load_document(str(base)).focus_tree["tree_id"] == "gen5"
    assert load_document(str(tmp_path / "auto.json.1")).focus_tree["tree_id"] == "gen4"
    assert load_document(str(tmp_path / "auto.json.2")).focus_tree["tree_id"] == "gen3"
    assert not (tmp_path / "auto.json.3").exists()


# --- recovery -------------------------------------------------------------

def _write_autosave(project_path: Path, name: str, tree_id: str) -> Path:
    autosave_dir = project_path.parent / "autosaves"
    autosave_dir.mkdir(exist_ok=True)
    target = autosave_dir / name
    save_document(str(target), new_document(tree_id), backups=False)
    return target


def test_recovery_prefers_newest_valid_not_newest_file(tmp_path):
    """Acceptance: recovery selects the newest *valid* document."""
    project = tmp_path / "p.json"
    project.write_text("{ this is not json", encoding="utf-8")

    older = _write_autosave(project, "p.json.1", "older_but_good")
    newer = _write_autosave(project, "p.json", "newer_but_corrupt")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    # Corrupt the newest autosave after the fact, as a crash mid-write would.
    newer.write_text('{"schema_version": 1, "focus_tree": ', encoding="utf-8")

    candidates = find_recovery_candidates(str(project))
    assert candidates[0].path == str(newer)  # newest *file*
    assert candidates[0].valid is False

    best = best_recovery_candidate(str(project))
    assert best is not None
    assert best.path == str(older)  # newest *valid*
    assert best.document.focus_tree["tree_id"] == "older_but_good"


def test_recovery_finds_backups_too(tmp_path):
    project = tmp_path / "p.json"
    save_document(str(project), new_document("v1"), backups=False)
    save_document(str(project), new_document("v2"))  # rotates v1 into .bak1
    project.write_text("corrupt", encoding="utf-8")

    best = best_recovery_candidate(str(project))
    assert best is not None
    assert best.kind == "backup"
    assert best.document.focus_tree["tree_id"] == "v1"


def test_recovery_returns_none_when_nothing_is_salvageable(tmp_path):
    project = tmp_path / "p.json"
    project.write_text("corrupt", encoding="utf-8")
    assert best_recovery_candidate(str(project)) is None


def test_recovery_candidate_explains_the_problem(tmp_path):
    project = tmp_path / "p.json"
    project.write_text(json.dumps({"version": "1.0.9", "focuses": [{"id": 5}]}),
                       encoding="utf-8")
    candidate = find_recovery_candidates(str(project))[0]
    assert candidate.valid is False
    assert "focus_tree.focuses[0].id" in candidate.problem_text
    assert candidate.timestamp_text != "unknown time"


# --- summaries ------------------------------------------------------------

def test_read_summary_handles_legacy_and_current(tmp_path):
    legacy_summary = read_summary(str(FIXTURES_DIR / "events.json"))
    assert legacy_summary is not None
    assert legacy_summary.event_count > 0

    current = tmp_path / "p.json"
    save_document(str(current), new_document("modern", "SOV"), backups=False)
    summary = read_summary(str(current))
    assert summary.tree_id == "modern"
    assert summary.country_tag == "SOV"


def test_read_summary_returns_none_for_non_project(tmp_path):
    other = tmp_path / "settings.json"
    other.write_text('"just a string"', encoding="utf-8")
    assert read_summary(str(other)) is None


def test_set_editor_preference_round_trips(tmp_path):
    path = tmp_path / "p.json"
    save_document(str(path), new_document("t"), backups=False)
    summary = set_editor_preference(path=str(path), key="prefer_app_settings", value=True)
    assert summary.prefer_app_settings is True
    assert load_document(str(path)).editor["preferences"]["prefer_app_settings"] is True


def test_set_editor_preference_on_legacy_file(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text((FIXTURES_DIR / "basic_focuses.json").read_text(encoding="utf-8"),
                    encoding="utf-8")
    summary = set_editor_preference(str(path), "prefer_app_settings", True)
    assert summary.prefer_app_settings is True
    # The migration must not have lost the focuses on the way through.
    assert summary.focus_count == 3
