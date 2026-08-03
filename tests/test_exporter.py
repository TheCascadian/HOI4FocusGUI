"""Smoke tests for HOI4 mod-format export (source/_exporter.py:
HOI4Exporter.export_tree), covering plain focuses, prerequisites, and
mutual exclusivity. Localisation/gfx writing is disabled since it needs
real icon assets; only the focus script (.txt) generation is exercised
here, which is the part relevant to the focus-tree data model.
"""

from pathlib import Path

from _dataStructs import Focus, FocusTree
from _exporter import HOI4Exporter


def _build_sample_tree() -> FocusTree:
    tree = FocusTree(id="sample_focus_tree", tree_name="sample_focus_tree", country_tag="GER")
    tree.focuses = [
        Focus(id="root", name="root", x=0, y=0, cost=10),
        Focus(
            id="army_path",
            name="army_path",
            x=-1,
            y=1,
            cost=7,
            prerequisites=["root"],
            mutually_exclusive=["navy_path"],
        ),
        Focus(
            id="navy_path",
            name="navy_path",
            x=1,
            y=1,
            cost=7,
            prerequisites=["root"],
            mutually_exclusive=["army_path"],
        ),
    ]
    return tree


def test_export_tree_writes_focus_script(tmp_path):
    tree = _build_sample_tree()
    exporter = HOI4Exporter()

    ok = exporter.export_tree(tree, str(tmp_path), write_localisation=False, write_gfx=False)
    assert ok is True

    summary = exporter.get_last_export_summary()
    assert summary is not None
    focus_path = Path(summary["focus_path"])
    assert focus_path.exists()

    content = focus_path.read_text(encoding="utf-8-sig")
    assert "focus_tree = {" in content
    assert "id = sample_focus_tree" in content


def test_export_tree_includes_all_focus_ids(tmp_path):
    tree = _build_sample_tree()
    exporter = HOI4Exporter()
    exporter.export_tree(tree, str(tmp_path), write_localisation=False, write_gfx=False)

    content = Path(exporter.get_last_export_summary()["focus_path"]).read_text(encoding="utf-8-sig")
    for focus in tree.focuses:
        assert f"id = {focus.id}" in content


def test_export_tree_writes_prerequisites():
    tree = _build_sample_tree()
    exporter = HOI4Exporter()
    content = exporter.tree_to_string(tree)

    assert "prerequisite = { focus = root }" in content


def test_export_tree_writes_mutually_exclusive():
    tree = _build_sample_tree()
    exporter = HOI4Exporter()
    content = exporter.tree_to_string(tree)

    assert "mutually_exclusive = { focus = navy_path }" in content
    assert "mutually_exclusive = { focus = army_path }" in content
