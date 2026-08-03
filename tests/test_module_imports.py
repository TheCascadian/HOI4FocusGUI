"""Smoke tests: every module under source/ must import cleanly in isolation.

The module list is discovered programmatically (glob over source/*.py) so
newly added modules are automatically covered without editing this file.
"""

import importlib
import sys
from pathlib import Path

import pytest

SOURCE_DIR = Path(__file__).resolve().parent.parent / "source"

# backup.py shadows the stdlib-adjacent name poorly and _mod_manager.py is a
# 4-line stub; both are still imported, nothing is excluded here. Modules
# are discovered dynamically -- do not hardcode a module list.
def _discover_modules():
    names = []
    for path in sorted(SOURCE_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        names.append(path.stem)
    return names


MODULE_NAMES = _discover_modules()


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_module_imports_cleanly(module_name):
    assert str(SOURCE_DIR) in sys.path
    module = importlib.import_module(module_name)
    assert module is not None


def test_discovered_at_least_the_expected_modules():
    # Sanity check that discovery actually found the known core modules,
    # guarding against a misconfigured glob silently collecting zero tests.
    expected_subset = {
        "_focusGUI",
        "_dataStructs",
        "_exporter",
        "_import_pipeline",
        "_txt_converter",
        "error_handler",
        "_imports",
        "_commands",
        "_nodes",
    }
    assert expected_subset.issubset(set(MODULE_NAMES))
