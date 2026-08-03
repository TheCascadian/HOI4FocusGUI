"""Project save/load round-trip smoke tests.

Exercises the real application code path: HOI4FocusTreeGenerator's
load_project_from_dict() (which builds FocusNode/EventNode/NoteNode
graphics items on the canvas from a project dict) and
_build_project_payload() (which serializes the live GUI state back to a
project dict, the same routine save_project() uses before
json.dump-ing to disk).

Each fixture project under tests/fixtures/projects/ is loaded into a
real (offscreen) main window, then re-serialized, and the meaningful
modeled fields are compared against the original fixture to confirm no
data is lost across a load -> save cycle. Fixtures deliberately omit
default-valued optional fields (matching the app's own space-saving
serialization in _build_project_payload), so comparisons only assert
on fields that were explicitly present in the fixture.
"""

import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog

from _focusGUI import HOI4FocusTreeGenerator
from _dialog import ProjectsHomeDialog

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "projects"
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.json"))


@pytest.fixture(autouse=True)
def _no_modal_projects_home_dialog(monkeypatch):
    monkeypatch.setattr(ProjectsHomeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    yield


@pytest.fixture
def window(qtbot):
    w = HOI4FocusTreeGenerator()
    qtbot.addWidget(w)
    yield w
    w.close()


@pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_project_loads_without_error(window, fixture_path):
    original = json.loads(fixture_path.read_text(encoding="utf-8"))
    window.load_project_from_dict(original)

    assert window.tree_id == original["tree_id"]
    assert window.country_tag == original["country_tag"]
    assert len(window.focuses) == len(original["focuses"])
    assert len(window.events) == len(original.get("events", []))

    loaded_ids = {f.id for f in window.focuses}
    expected_ids = {f["id"] for f in original["focuses"]}
    assert loaded_ids == expected_ids


@pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_project_round_trips_focus_fields(window, fixture_path):
    original = json.loads(fixture_path.read_text(encoding="utf-8"))
    window.load_project_from_dict(original)

    rebuilt = window._build_project_payload()

    assert rebuilt["tree_id"] == original["tree_id"]
    assert rebuilt["country_tag"] == original["country_tag"]

    rebuilt_focuses = {f["id"]: f for f in rebuilt["focuses"]}
    for orig_focus in original["focuses"]:
        fid = orig_focus["id"]
        assert fid in rebuilt_focuses, f"focus {fid} lost on round-trip"
        rt_focus = rebuilt_focuses[fid]
        # Only compare fields that were explicitly authored in the fixture;
        # _build_project_payload omits default-valued fields to save space
        # (notably x/y == 0, since 0 is the loader's own default -- see
        # _build_project_payload's "only include coordinates if not zero"
        # comment in source/_focusGUI.py).
        for key, value in orig_focus.items():
            if key == "id":
                continue
            if key in ("x", "y") and value == 0:
                continue
            assert rt_focus.get(key) == value, (
                f"focus '{fid}' field '{key}' changed on round-trip: "
                f"expected {value!r}, got {rt_focus.get(key)!r}"
            )


def test_events_fixture_round_trips_event_fields(window):
    fixture_path = FIXTURES_DIR / "events.json"
    original = json.loads(fixture_path.read_text(encoding="utf-8"))
    window.load_project_from_dict(original)

    assert len(window.events) == 1
    ev = window.events[0]
    assert ev.id == "news.1"
    assert ev.title == "A Grave Decision"
    assert ev.trigger == "is_puppet = no"
    assert "option" in ev.options_block

    rebuilt = window._build_project_payload()
    rebuilt_events = {e["id"]: e for e in rebuilt["events"]}
    assert "news.1" in rebuilt_events
    assert rebuilt_events["news.1"]["title"] == "A Grave Decision"
    assert rebuilt_events["news.1"]["trigger"] == "is_puppet = no"


def test_unknown_blocks_fixture_preserves_raw_unparsed(window):
    fixture_path = FIXTURES_DIR / "unknown_blocks.json"
    original = json.loads(fixture_path.read_text(encoding="utf-8"))
    window.load_project_from_dict(original)

    focus = next(f for f in window.focuses if f.id == "future_focus")
    assert focus.has_unparsed is True
    assert focus.raw_unparsed == original["focuses"][0]["raw_unparsed"]
    assert focus.clean_raw == original["focuses"][0]["clean_raw"]

    rebuilt = window._build_project_payload()
    rebuilt_focus = next(f for f in rebuilt["focuses"] if f["id"] == "future_focus")
    assert rebuilt_focus["raw_unparsed"] == original["focuses"][0]["raw_unparsed"]
    assert rebuilt_focus["has_unparsed"] is True


def test_notes_fixture_round_trips_note_and_link(window):
    fixture_path = FIXTURES_DIR / "frames_and_notes.json"
    original = json.loads(fixture_path.read_text(encoding="utf-8"))
    window.load_project_from_dict(original)

    assert len(window.canvas._notes_items) == 1
    note = window.canvas._notes_items[0]
    assert note.note_id == "note-1"

    # note -> focus link should have been (deferred-)applied once focuses exist
    assert len(window.canvas._note_focus_links) == 1

    rebuilt = window._build_project_payload()
    assert len(rebuilt["notes"]) == 1
    assert rebuilt["notes"][0]["id"] == "note-1"
    assert rebuilt["note_focus_links"] == [["note-1", "focus_one"]]
