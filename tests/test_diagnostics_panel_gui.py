"""Integration tests for the diagnostics panel and its editor wiring.

Covers the two things the panel exists to do -- list findings without
freezing, and turn a selected finding into a canvas selection -- plus the
export gate's error/warning policy.
"""

import pytest
from PyQt6.QtWidgets import QMessageBox

from _dataStructs import Focus, FocusTree
from _diagnostics_panel import SEVERITY_COLORS, DiagnosticsPanel
from _validation import (
    Diagnostic,
    DiagnosticCode,
    Severity,
    ValidationResult,
    validate_focus_tree,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def clean_focus(focus_id, **overrides):
    defaults = dict(
        name=f"Name {focus_id}",
        description=f"Desc {focus_id}",
        icon=f"{focus_id}.png",
        cost=10,
    )
    defaults.update(overrides)
    return Focus(id=focus_id, **defaults)


@pytest.fixture
def broken_tree():
    """A tree with one error, one warning, and one informational finding."""
    return FocusTree(
        id="test_tree",
        tree_name="test_tree",
        focuses=[
            clean_focus("root"),
            clean_focus("broken", prerequisites=["ghost"]),  # error + unreachable
            clean_focus("plain", prerequisites=["root"], icon=None, description=""),
        ],
    )


def place_focus(window, focus_id, x, y):
    """Put a focus on the editor's canvas, bypassing the modal add dialog."""
    focus = clean_focus(focus_id, x=x, y=y)
    window.focuses.append(focus)
    window.canvas.add_focus_node(focus)
    return focus


@pytest.fixture
def panel(qtbot):
    widget = DiagnosticsPanel()
    qtbot.addWidget(widget)
    return widget


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_panel_lists_one_row_per_diagnostic(panel, broken_tree):
    result = panel.validate_now(broken_tree)
    assert result.diagnostics
    assert panel.row_count() == len(result.diagnostics)


def test_panel_summarises_counts_by_severity(panel, broken_tree):
    result = panel.validate_now(broken_tree)
    counts = result.counts()
    text = panel.summary_label.text()
    assert f"{counts[Severity.ERROR]} errors" in text
    assert f"{counts[Severity.WARNING]} warnings" in text


def test_a_clean_tree_says_so(panel):
    tree = FocusTree(id="t", focuses=[clean_focus("a")])
    panel.validate_now(tree)
    assert panel.row_count() == 0
    assert "No problems" in panel.summary_label.text()


def test_severities_are_visually_distinguished(panel, broken_tree):
    """Errors, warnings, and info must not read the same at a glance."""
    panel.validate_now(broken_tree)
    seen = {
        panel.tree.topLevelItem(i).foreground(0).color().name()
        for i in range(panel.row_count())
    }
    assert len(seen) > 1
    # Each severity has its own colour; none share one.
    assert len({c.name() for c in SEVERITY_COLORS.values()}) == 3


def test_severity_filters_hide_rows_without_discarding_them(panel, broken_tree):
    result = panel.validate_now(broken_tree)
    total = panel.row_count()

    panel.show_warnings.setChecked(False)
    panel.show_infos.setChecked(False)
    assert panel.visible_row_count() == len(result.errors)
    # Filtering is a view concern; the underlying rows survive.
    assert panel.row_count() == total

    panel.show_warnings.setChecked(True)
    panel.show_infos.setChecked(True)
    assert panel.visible_row_count() == total


# ---------------------------------------------------------------------------
# Incremental execution
# ---------------------------------------------------------------------------


def test_validation_runs_incrementally_rather_than_in_one_blocking_call(panel):
    """start_validation must return before the work is done."""
    focuses = [clean_focus("root", x=0, y=0)]
    focuses += [
        clean_focus(f"f{i}", prerequisites=["ghost"], x=0, y=0) for i in range(1, 1500)
    ]
    tree = FocusTree(id="big", focuses=focuses)

    panel.start_validation(tree)
    assert panel.is_running
    assert panel.last_result is None


def test_incremental_run_completes_and_emits_its_result(panel, qtbot, broken_tree):
    with qtbot.waitSignal(panel.validation_finished, timeout=5000) as blocker:
        panel.start_validation(broken_tree)
    result = blocker.args[0]
    assert isinstance(result, ValidationResult)
    assert result.blocks_export
    assert panel.row_count() == len(result.diagnostics)
    assert not panel.is_running


def test_incremental_and_synchronous_runs_agree(panel, qtbot, broken_tree):
    synchronous = panel.validate_now(broken_tree)
    with qtbot.waitSignal(panel.validation_finished, timeout=5000) as blocker:
        panel.start_validation(broken_tree)
    incremental = blocker.args[0]
    assert [d.to_dict() for d in synchronous] == [d.to_dict() for d in incremental]


def test_starting_a_run_discards_the_previous_one(panel, qtbot, broken_tree):
    """Findings from a superseded run must never reach the view."""
    big = FocusTree(
        id="big",
        focuses=[clean_focus(f"f{i}", prerequisites=["ghost"]) for i in range(1500)],
    )
    panel.start_validation(big)
    assert panel.is_running

    with qtbot.waitSignal(panel.validation_finished, timeout=5000):
        panel.start_validation(broken_tree)

    expected = validate_focus_tree(broken_tree)
    assert panel.row_count() == len(expected.diagnostics)


def test_repeated_validation_does_not_accumulate_rows(panel, broken_tree):
    """The panel is not a log; each run replaces the last."""
    counts = {len(panel.validate_now(broken_tree)) and panel.row_count() for _ in range(6)}
    assert len(counts) == 1


def test_a_rule_that_raises_ends_the_run_instead_of_spinning(panel, monkeypatch):
    """A validator defect must not leave the panel stuck at 'Validating...'."""

    def exploding_stream(*_args, **_kwargs):
        yield Diagnostic(
            code=DiagnosticCode.ICON_MISSING,
            severity=Severity.WARNING,
            message="first",
            object_id="a",
        )
        raise RuntimeError("rule defect")

    monkeypatch.setattr(
        "_diagnostics_panel.iter_validate_focus_tree", exploding_stream
    )
    result = panel.validate_now(FocusTree(id="t", focuses=[clean_focus("a")]))

    assert not panel.is_running
    # What was produced before the failure is still shown.
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_selecting_a_row_emits_its_diagnostic(panel, qtbot, broken_tree):
    panel.validate_now(broken_tree)
    with qtbot.waitSignal(panel.diagnostic_activated, timeout=1000) as blocker:
        panel.select_row(0)
    assert isinstance(blocker.args[0], Diagnostic)
    assert blocker.args[0] is panel.selected_diagnostic()


def test_the_first_row_is_the_first_error(panel, broken_tree):
    """Errors sort first, so the top row is the most urgent problem."""
    panel.validate_now(broken_tree)
    assert panel.tree.topLevelItem(0).text(0) == "Error"


def test_show_result_displays_a_precomputed_run(panel):
    result = ValidationResult(
        diagnostics=[
            Diagnostic(
                code=DiagnosticCode.PREREQ_MISSING,
                severity=Severity.ERROR,
                message="precomputed",
                object_id="a",
            )
        ]
    )
    panel.show_result(result)
    assert panel.row_count() == 1
    assert panel.last_result is result


# ---------------------------------------------------------------------------
# Editor wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_modal_projects_home_dialog(monkeypatch):
    """Neutralise the modal project picker baked into main-window __init__.

    Same workaround as tests/test_main_window.py: setup_ui() unconditionally
    exec()s ProjectsHomeDialog, which blocks forever under pytest because
    nothing clicks it. Pre-existing production behaviour, worked around in
    tests only.
    """
    from PyQt6.QtWidgets import QDialog

    from _dialog import ProjectsHomeDialog

    monkeypatch.setattr(
        ProjectsHomeDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    yield


@pytest.fixture
def window(qtbot, _no_modal_projects_home_dialog):
    from _focusGUI import HOI4FocusTreeGenerator

    win = HOI4FocusTreeGenerator()
    qtbot.addWidget(win)
    yield win
    win.close()


def test_editor_owns_a_hidden_diagnostics_dock(window):
    assert isinstance(window.diagnostics_panel, DiagnosticsPanel)
    assert window.diagnostics_panel.isHidden()


def test_validation_tree_snapshots_editor_state(window):
    window.tree_id = "my_tree"
    window.focuses = [clean_focus("a")]
    tree = window.build_validation_tree()
    assert tree.id == "my_tree"
    assert [f.id for f in tree.focuses] == ["a"]


def test_selecting_a_diagnostic_selects_and_centres_the_focus(window, qtbot):
    """The integration the panel exists for: finding -> focus on canvas."""
    place_focus(window, "alpha", 0, 0)
    target = place_focus(window, "beta", 5, 5)

    node = window.canvas.nodes[target.id]
    other = window.canvas.nodes["alpha"]
    other.setSelected(True)

    window._on_diagnostic_activated(
        Diagnostic(
            code=DiagnosticCode.ICON_MISSING,
            severity=Severity.WARNING,
            message="no icon",
            object_id=target.id,
        )
    )

    assert node.isSelected()
    assert not other.isSelected()


def test_navigating_to_a_deleted_focus_reports_failure(window):
    assert window.navigate_to_focus("does_not_exist") is False


def test_tree_level_diagnostics_leave_the_selection_alone(window):
    """A finding with no object_id has nothing to navigate to."""
    focus = place_focus(window, "alpha", 0, 0)
    node = window.canvas.nodes[focus.id]
    node.setSelected(True)

    window._on_diagnostic_activated(
        Diagnostic(
            code=DiagnosticCode.TREE_HAS_NO_ROOT,
            severity=Severity.WARNING,
            message="no root",
            object_id=None,
        )
    )
    assert node.isSelected()


def test_showing_the_panel_reveals_it_and_validates(window, qtbot):
    """Manual validation is asynchronous, so the menu action never blocks."""
    window.focuses = [clean_focus("a", prerequisites=["ghost"])]
    panel = window.diagnostics_panel

    with qtbot.waitSignal(panel.validation_finished, timeout=5000) as blocker:
        window.show_diagnostics_panel()

    assert not panel.isHidden()
    assert blocker.args[0].blocks_export
    assert panel.last_result.by_code(DiagnosticCode.PREREQ_MISSING)


# ---------------------------------------------------------------------------
# Export gate
# ---------------------------------------------------------------------------


def test_warnings_alone_never_block_export(window, monkeypatch):
    def fail_if_asked(*_args, **_kwargs):
        raise AssertionError("warnings must not prompt the user")

    monkeypatch.setattr(QMessageBox, "warning", fail_if_asked)
    window.focuses = [clean_focus("a", icon=None, description="", cost=0)]
    window.tree_id = "t"

    assert window.validate_before_export() is True


def test_errors_block_export_unless_the_user_insists(window, monkeypatch):
    window.focuses = [clean_focus("a", prerequisites=["ghost"])]
    window.tree_id = "t"

    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.No
    )
    assert window.validate_before_export() is False

    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    assert window.validate_before_export() is True


def test_the_export_gate_shows_the_findings_in_the_panel(window, monkeypatch):
    """Errors go to the dock, not into a modal list the user cannot click."""
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.No
    )
    window.focuses = [clean_focus("a", prerequisites=["ghost"])]
    window.tree_id = "t"
    window.validate_before_export()

    panel = window.diagnostics_panel
    assert not panel.isHidden()
    assert panel.last_result.by_code(DiagnosticCode.PREREQ_MISSING)


def test_a_clean_tree_exports_without_any_prompt(window, monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )
    window.focuses = [clean_focus("a")]
    window.tree_id = "t"
    assert window.validate_before_export() is True
