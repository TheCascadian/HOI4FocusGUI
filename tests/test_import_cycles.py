"""Regression tests proving the core modules are free of circular imports.

Scope: ``_imports``, ``_dataStructs``, ``_utils``, ``_exporter`` and
``_import_pipeline``. These modules used to break each other on import: the
facade ``_imports`` imported ``_dataStructs``/``_utils`` while both of them
imported ``_imports`` back, and the cycle was papered over with the
``FOCUS_SKIP_DATASTRUCTS`` / ``FOCUS_SKIP_UTILS`` environment variables plus
``except ImportError: X = None`` placeholders. Consequences were that the set of
usable symbols depended on which module Python happened to import first, and a
genuine breakage surfaced far away as ``TypeError: 'NoneType' object is not
callable``.

Every test that cares about import order runs in a **fresh subprocess**, because
``sys.modules`` is global and already fully populated by the time pytest has
collected the rest of the suite -- an in-process test would prove nothing.
"""

import ast
import itertools
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "source"

# The five modules this change covers.
CORE_MODULES = [
    "_imports",
    "_dataStructs",
    "_utils",
    "_exporter",
    "_import_pipeline",
]

# Symbols each module must expose regardless of import order. These are the
# names that previously could silently become ``None``.
EXPECTED_SYMBOLS = {
    "_dataStructs": ["Focus", "Event", "CommandSpec", "FocusBranch", "FocusTree"],
    "_utils": [
        "clone_focus_pure",
        "draw_outlined_text",
        "format_project_file_size",
        "obfuscate_path",
        "obfuscate_text",
        "obfuscate_user_in_path",
        "pixmap_from_file_via_pillow",
        "safe_qt_call",
        "safe_ui_operation",
        "set_widget_path_display",
        "show_error",
    ],
    "_imports": [
        "Focus",
        "Event",
        "CommandSpec",
        "show_error",
        "clone_focus_pure",
        "convert_txt_to_project_dict",
    ],
    "_exporter": ["HOI4Exporter"],
    "_import_pipeline": ["ImportPipeline"],
}


def run_in_subprocess(body: str) -> subprocess.CompletedProcess:
    """Execute ``body`` in a fresh interpreter with ``source/`` importable.

    The child inherits a headless Qt platform and a scratch HOME so that
    importing GUI-adjacent modules neither opens a window nor writes to the
    real user profile.
    """
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SOURCE_DIR)!r})
        """
    ) + textwrap.dedent(body)

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )


def parse_module(module_name: str) -> ast.Module:
    """Parse a source module so tests inspect real syntax, not docstring prose."""
    path = SOURCE_DIR / f"{module_name}.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_module_names(tree: ast.Module) -> set:
    """Return every module name reachable from an ``import``/``from`` statement."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def assert_ok(result: subprocess.CompletedProcess, context: str) -> None:
    assert result.returncode == 0, (
        f"{context} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# --------------------------------------------------------------------------
# Import order independence
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first_module", CORE_MODULES)
def test_module_imports_first_in_fresh_process(first_module):
    """Each core module must import cleanly as the very first project import."""
    result = run_in_subprocess(
        f"""
        import {first_module}
        print("ok")
        """
    )
    assert_ok(result, f"importing {first_module} first")


@pytest.mark.parametrize(
    "order",
    [pytest.param(o, id="-".join(m.lstrip("_") for m in o)) for o in itertools.permutations(CORE_MODULES, 3)],
)
def test_three_module_permutations_import_cleanly(order):
    """No 3-module import sequence may fail or change what a module exposes.

    All 60 ordered triples are exercised; with a cycle present, the orders that
    reach ``_imports`` through a partially-initialised ``_dataStructs`` or
    ``_utils`` are the ones that used to produce ``None`` placeholders.
    """
    order_desc = " -> ".join(order)
    checks = "\n".join(
        f"import {name}\n"
        + "\n".join(
            f"assert getattr({name}, {sym!r}, None) is not None, "
            f"{f'{name}.{sym} is None after import order {order_desc}'!r}"
            for sym in EXPECTED_SYMBOLS[name]
        )
        for name in order
    )
    result = run_in_subprocess(checks + '\nprint("ok")\n')
    assert_ok(result, f"import order {list(order)}")


def test_symbol_identity_is_order_independent():
    """``Focus`` reached via either path must be the *same* class object.

    Under the old cycle, ``_imports.Focus`` could be ``None`` while
    ``_dataStructs.Focus`` was the real class, so isinstance checks silently
    disagreed depending on which module a caller had imported from.
    """
    forward = run_in_subprocess(
        """
        import _dataStructs, _imports, _utils
        print(_imports.Focus is _dataStructs.Focus)
        print(_imports.show_error is _utils.show_error)
        """
    )
    assert_ok(forward, "forward order identity check")

    reverse = run_in_subprocess(
        """
        import _utils, _imports, _dataStructs
        print(_imports.Focus is _dataStructs.Focus)
        print(_imports.show_error is _utils.show_error)
        """
    )
    assert_ok(reverse, "reverse order identity check")

    assert forward.stdout.split() == ["True", "True"]
    assert reverse.stdout.split() == ["True", "True"]


def test_wildcard_import_from_facade_still_works():
    """``from _imports import *`` remains valid for not-yet-converted modules."""
    result = run_in_subprocess(
        """
        exec("from _imports import *")
        ns = dict(globals())
        assert ns["Focus"] is not None
        assert ns["show_error"] is not None
        assert ns["convert_txt_to_project_dict"] is not None
        print("ok")
        """
    )
    assert_ok(result, "wildcard import from facade")


# --------------------------------------------------------------------------
# The workarounds must stay gone
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_no_skip_environment_variables_in_source(module_name):
    """The FOCUS_SKIP_* import workaround must not reappear."""
    text = (SOURCE_DIR / f"{module_name}.py").read_text(encoding="utf-8")
    assert "FOCUS_SKIP_DATASTRUCTS" not in text
    assert "FOCUS_SKIP_UTILS" not in text


def test_modules_import_without_skip_env_vars_set():
    """Importing must not depend on environment-variable manipulation.

    The child explicitly sets both flags to "1"; under the old scheme that made
    the facade publish ``None`` for every project symbol.
    """
    result = run_in_subprocess(
        """
        import os
        os.environ["FOCUS_SKIP_DATASTRUCTS"] = "1"
        os.environ["FOCUS_SKIP_UTILS"] = "1"
        import _imports
        assert _imports.Focus is not None
        assert _imports.show_error is not None
        assert _imports.clone_focus_pure is not None
        print("ok")
        """
    )
    assert_ok(result, "import with FOCUS_SKIP_* set")


@pytest.mark.parametrize("module_name", ["_dataStructs", "_utils", "_exporter", "_import_pipeline"])
def test_converted_modules_do_not_import_the_facade(module_name):
    """The four converted modules must not import ``_imports`` (directly)."""
    assert "_imports" not in imported_module_names(parse_module(module_name)), (
        f"{module_name} still imports the _imports facade"
    )


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_no_wildcard_imports(module_name):
    """No ``from X import *`` anywhere in the five modules.

    Checked via AST so the usage examples in the ``_imports`` docstring do not
    produce a false positive.
    """
    tree = parse_module(module_name)
    offenders = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
    ]
    assert not offenders, f"{module_name} still uses wildcard imports from: {offenders}"


# --------------------------------------------------------------------------
# Layering guarantees
# --------------------------------------------------------------------------

def test_dataStructs_does_not_pull_in_qt():
    """Domain dataclasses must be usable without PyQt6 being imported at all."""
    result = run_in_subprocess(
        """
        import sys
        import _dataStructs

        qt = sorted(m for m in sys.modules if m.startswith("PyQt6"))
        assert not qt, f"_dataStructs pulled in PyQt6 modules: {qt}"
        assert "_imports" not in sys.modules, "_dataStructs pulled in the _imports facade"
        assert "_utils" not in sys.modules, "_dataStructs pulled in _utils"

        focus = _dataStructs.Focus(id="focus_a", name="Focus A", prerequisites="focus_b")
        assert focus.prerequisites == ["focus_b"]
        tree = _dataStructs.FocusTree(id="tree", focuses=[focus])
        assert _dataStructs.FocusTree.from_dict(tree.to_dict()).focuses[0].id == "focus_a"
        print("ok")
        """
    )
    assert_ok(result, "_dataStructs Qt-free import")


def test_dataStructs_importable_when_pyqt6_is_unavailable():
    """Hard proof of Qt independence: make ``import PyQt6`` fail outright."""
    result = run_in_subprocess(
        """
        import sys

        class _BlockPyQt6:
            def find_module(self, name, path=None):
                return None
            def find_spec(self, name, path=None, target=None):
                if name == "PyQt6" or name.startswith("PyQt6."):
                    raise ImportError("PyQt6 is blocked for this test")
                return None

        sys.meta_path.insert(0, _BlockPyQt6())

        import _dataStructs
        assert _dataStructs.Focus(id="x").id == "x"
        print("ok")
        """
    )
    assert_ok(result, "_dataStructs import with PyQt6 blocked")


def test_exporter_does_not_pull_in_the_facade_or_gui():
    result = run_in_subprocess(
        """
        import sys
        import _exporter

        for forbidden in ("_imports", "_focusGUI", "_dialog"):
            assert forbidden not in sys.modules, f"_exporter pulled in {forbidden}"
        print("ok")
        """
    )
    assert_ok(result, "_exporter layering")


def test_import_pipeline_does_not_pull_in_the_gui():
    """``_import_pipeline`` must not reach the GUI layer.

    It does still reach ``_imports`` *transitively*, because its dependency
    ``_txt_converter`` has not been converted to explicit imports yet -- that
    module is outside the scope of this change. What matters here is that the
    edge is one-way and terminates: ``_imports`` no longer imports
    ``_txt_converter`` back at module scope, so there is no cycle.
    """
    result = run_in_subprocess(
        """
        import sys
        import _import_pipeline

        for forbidden in ("_focusGUI", "_dialog", "_state_viewport", "_nodes"):
            assert forbidden not in sys.modules, f"_import_pipeline pulled in {forbidden}"
        print("ok")
        """
    )
    assert_ok(result, "_import_pipeline layering")


def test_import_pipeline_does_not_import_the_facade_directly():
    assert "_imports" not in imported_module_names(parse_module("_import_pipeline"))


def test_facade_does_not_import_txt_converter_at_module_scope():
    """``_imports`` must resolve ``_txt_converter`` lazily, breaking the cycle.

    ``_txt_converter`` imports ``_imports``; if ``_imports`` imported it back at
    module scope the pair would deadlock on whichever was imported first.
    """
    result = run_in_subprocess(
        """
        import sys
        import _imports

        assert "_txt_converter" not in sys.modules, (
            "_imports imported _txt_converter eagerly, re-creating the cycle"
        )
        # ...but the name still resolves on demand for legacy callers.
        assert _imports.convert_txt_to_project_dict is not None
        assert "_txt_converter" in sys.modules
        print("ok")
        """
    )
    assert_ok(result, "lazy _txt_converter resolution")


def test_txt_converter_first_then_facade():
    """The formerly-deadlocking order: converter first, facade second."""
    result = run_in_subprocess(
        """
        import _txt_converter
        import _imports
        assert _imports.convert_txt_to_project_dict is _txt_converter.convert_txt_to_project_dict
        print("ok")
        """
    )
    assert_ok(result, "_txt_converter then _imports")


def test_utils_does_not_pull_in_the_gui():
    """``_utils`` may use Qt, but must not reach back into the GUI layer."""
    result = run_in_subprocess(
        """
        import sys
        import _utils

        for forbidden in ("_imports", "_focusGUI", "_dialog", "_state_viewport"):
            assert forbidden not in sys.modules, f"_utils pulled in {forbidden}"
        print("ok")
        """
    )
    assert_ok(result, "_utils layering")


def test_utils_source_has_no_focusgui_import():
    """No ``_focusGUI`` import survives in ``_utils`` -- not even a lazy one.

    The old ``show_error`` did ``from _focusGUI import ErrorDialog`` inside the
    function body, which is why the AST walk covers nested scopes too.
    """
    assert "_focusGUI" not in imported_module_names(parse_module("_utils")), (
        "_utils still imports _focusGUI"
    )


# --------------------------------------------------------------------------
# Injectable error presenter (replaces the _utils -> _focusGUI reverse edge)
# --------------------------------------------------------------------------

def test_error_presenter_module_is_dependency_neutral():
    result = run_in_subprocess(
        """
        import sys
        import _error_presenter

        qt = sorted(m for m in sys.modules if m.startswith("PyQt6"))
        assert not qt, f"_error_presenter pulled in PyQt6: {qt}"
        assert _error_presenter.get_error_presenter() is None
        print("ok")
        """
    )
    assert_ok(result, "_error_presenter neutrality")


def test_show_error_delegates_to_registered_presenter():
    import _error_presenter
    import _utils

    calls = []

    def presenter(parent, title, message, details=None):
        calls.append((parent, title, message, details))
        return True

    previous = _error_presenter.get_error_presenter()
    _error_presenter.set_error_presenter(presenter)
    try:
        _utils.show_error(None, "Save Failed", "Could not save project")
    finally:
        if previous is None:
            _error_presenter.clear_error_presenter()
        else:
            _error_presenter.set_error_presenter(previous)

    assert len(calls) == 1
    assert calls[0][1] == "Save Failed"
    assert calls[0][2] == "Could not save project"


def test_show_error_falls_back_when_presenter_declines(monkeypatch):
    import _error_presenter
    import _utils

    fallback = []
    monkeypatch.setattr(
        _utils.QMessageBox,
        "critical",
        staticmethod(lambda parent, title, text: fallback.append((title, text))),
    )

    previous = _error_presenter.get_error_presenter()
    _error_presenter.set_error_presenter(lambda parent, title, message, details=None: False)
    try:
        _utils.show_error(None, "Nope", "declined message")
    finally:
        if previous is None:
            _error_presenter.clear_error_presenter()
        else:
            _error_presenter.set_error_presenter(previous)

    assert fallback == [("Nope", "declined message")]


def test_show_error_survives_a_broken_presenter(monkeypatch):
    """A presenter that raises must not propagate out of ``show_error``."""
    import _error_presenter
    import _utils

    fallback = []
    monkeypatch.setattr(
        _utils.QMessageBox,
        "critical",
        staticmethod(lambda parent, title, text: fallback.append((title, text))),
    )

    def boom(parent, title, message, details=None):
        raise RuntimeError("presenter exploded")

    previous = _error_presenter.get_error_presenter()
    _error_presenter.set_error_presenter(boom)
    try:
        _utils.show_error(None, "Boom", "broken presenter")
    finally:
        if previous is None:
            _error_presenter.clear_error_presenter()
        else:
            _error_presenter.set_error_presenter(previous)

    assert fallback == [("Boom", "broken presenter")]


def test_set_error_presenter_rejects_non_callables():
    import _error_presenter

    with pytest.raises(TypeError):
        _error_presenter.set_error_presenter("not callable")


# --------------------------------------------------------------------------
# No placeholder Nones, no suppressed ImportError
# --------------------------------------------------------------------------

def test_facade_raises_instead_of_publishing_none(tmp_path):
    """A broken ``_dataStructs`` must make ``import _imports`` fail loudly.

    Previously the failure was swallowed and ``_imports.Focus`` became ``None``,
    turning an import bug into a confusing ``NoneType`` error much later.
    """
    shim = tmp_path / "_dataStructs.py"
    shim.write_text("raise ImportError('simulated broken _dataStructs')\n", encoding="utf-8")

    result = run_in_subprocess(
        f"""
        import sys
        sys.path.insert(0, {str(tmp_path)!r})
        try:
            import _imports
        except ImportError as exc:
            print("raised:", exc)
        else:
            print("SILENTLY SURVIVED, Focus =", _imports.Focus)
        """
    )
    assert_ok(result, "broken _dataStructs propagation")
    assert "raised:" in result.stdout, result.stdout
    assert "SILENTLY SURVIVED" not in result.stdout


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_no_none_placeholder_assignments(module_name):
    """Guard against the ``except ImportError: X = None`` pattern returning."""
    text = (SOURCE_DIR / f"{module_name}.py").read_text(encoding="utf-8")
    assert "except ImportError" not in text, (
        f"{module_name} suppresses ImportError; failures must propagate"
    )
