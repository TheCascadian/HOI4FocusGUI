"""Shared pytest fixtures/configuration for the HOI4FocusGUI test suite.

Key responsibilities of this file:

1. Force headless Qt execution (``QT_QPA_PLATFORM=offscreen``) *before* any
   PyQt6 module is imported anywhere in the test session -- including by
   pytest-qt itself. This must happen at very first import time of this
   file, which pytest guarantees happens before test collection imports
   the modules under test.
2. Make sure ``source/`` is importable as a flat module namespace, since
   the application has no package ``__init__.py`` and every module in
   ``source/`` imports its siblings with bare ``import xxx`` / ``from xxx
   import yyy`` statements (e.g. ``from _imports import (...)``).
3. Keep the test run from touching real user directories. The app
   normally stores settings/projects/logs under the platform app-data
   directory (``~/.local/share/focus_tool`` on Linux,
   ``%LOCALAPPDATA%/FocusTool`` on Windows) or under ``./Logs`` relative
   to the current working directory. Tests redirect all of these into a
   throwaway per-session temp directory via environment variables that
   the production code already honours (``XDG_DATA_HOME``,
   ``FOCUS_LOG_DIR``) plus a couple of monkeypatched Windows env vars for
   completeness on that platform.
"""

import os
import sys
import tempfile
from pathlib import Path

# --- 1. Headless Qt -------------------------------------------------------
# Must be set before "import PyQt6.QtWidgets" (or anything that imports it)
# happens anywhere, on any platform (including one with a real display).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 2. Make source/ importable --------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

# --- 3. Redirect all persistent app state into a session-scoped temp dir ---
# Done at import time (not just in a fixture) so that even module-level
# code executed during collection/import never touches the real user
# profile. Individual tests may further override with tmp_path as needed.
_SESSION_STATE_DIR = tempfile.mkdtemp(prefix="hoi4focusgui-test-state-")
os.environ["XDG_DATA_HOME"] = str(Path(_SESSION_STATE_DIR) / "xdg_data")
os.environ["LOCALAPPDATA"] = str(Path(_SESSION_STATE_DIR) / "local_appdata")
os.environ["APPDATA"] = str(Path(_SESSION_STATE_DIR) / "appdata")
os.environ["FOCUS_LOG_DIR"] = str(Path(_SESSION_STATE_DIR) / "logs")
for _p in (
    os.environ["XDG_DATA_HOME"],
    os.environ["LOCALAPPDATA"],
    os.environ["APPDATA"],
    os.environ["FOCUS_LOG_DIR"],
):
    os.makedirs(_p, exist_ok=True)

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Belt-and-braces: also point Path.home() at a throwaway dir for the
    duration of each test, in case any code path falls back to it instead
    of honouring XDG_DATA_HOME/LOCALAPPDATA/APPDATA.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    yield
