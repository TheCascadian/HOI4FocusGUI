"""Smoke test: the main window constructs and tears down cleanly.

This exercises HOI4FocusTreeGenerator.__init__ (source/_focusGUI.py), which
in turn builds the whole UI (menus, docks, the FocusTreeCanvas, etc.),
loads persisted settings/database (redirected to a throwaway temp dir by
tests/conftest.py), and scans the icon library. None of this should raise,
and no leftover top-level widgets should remain after close().
"""

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from _focusGUI import HOI4FocusTreeGenerator, FocusTreeCanvas
from _dialog import ProjectsHomeDialog


@pytest.fixture(autouse=True)
def _no_modal_projects_home_dialog(monkeypatch):
    """HOI4FocusTreeGenerator.__init__() -> setup_ui() unconditionally calls
    show_projects_home(), which does ``ProjectsHomeDialog(...).exec()``.
    That is a real (blocking, modal) QDialog.exec() call -- fine for an
    interactive user, but it hangs forever under pytest since nothing ever
    clicks it. This is a discovered production behavior (main window
    construction is not test-friendly / has a UI side effect baked into
    __init__), not something we fix in source. We work around it in tests
    only by making the dialog's exec() a no-op that reports "rejected",
    exactly as if the user closed it without picking a project.
    """
    monkeypatch.setattr(ProjectsHomeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    yield


def test_main_window_constructs_and_closes(qtbot):
    window = HOI4FocusTreeGenerator()
    qtbot.addWidget(window)

    # Basic sanity checks on core state established during __init__.
    assert window.focuses == []
    assert window.events == []
    assert isinstance(window.canvas, FocusTreeCanvas)
    assert window.tree_id
    assert window.country_tag

    window.show()
    qtbot.waitExposed(window)

    window.close()


def test_main_window_app_base_dir_is_isolated(qtbot, tmp_path, monkeypatch):
    # Guard against accidentally writing into the real user profile: the
    # autouse fixture in conftest.py points XDG_DATA_HOME/HOME at temp
    # dirs, so app_base_dir must land under one of those, never under the
    # real repo checkout or the actual user's home.
    window = HOI4FocusTreeGenerator()
    qtbot.addWidget(window)

    assert window.app_base_dir is not None
    # Must not be the developer's real home/.local/share.
    import os
    real_home = os.path.expanduser("~")
    # Since HOME was monkeypatched, os.path.expanduser inside this test
    # process reflects the same override used by the app.
    assert str(window.app_base_dir).startswith(os.environ.get("HOME", real_home)) or \
        str(window.app_base_dir).startswith(os.environ.get("XDG_DATA_HOME", ""))

    window.close()
