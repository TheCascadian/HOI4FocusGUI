"""Unit tests for the pure focus-tree validator (source/_validation.py).

One test per diagnostic code, plus the properties the subsystem promises:
determinism, Qt independence, and a catalog that stays in sync with the
codes the rules actually emit.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from _dataStructs import Focus, FocusTree
from _validation import (
    DIAGNOSTIC_CATALOG,
    Diagnostic,
    DiagnosticCode,
    Severity,
    SourceLocation,
    ValidationContext,
    validate_focus_tree,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_focus(focus_id, **overrides):
    """A focus that is clean by default, so each test isolates one rule.

    Without this, every fixture would trip the missing-icon and
    missing-description rules and the assertions would be about noise.
    """
    defaults = dict(
        name=f"Name of {focus_id}",
        description=f"Description of {focus_id}",
        icon=f"{focus_id}.png",
        cost=10,
    )
    defaults.update(overrides)
    return Focus(id=focus_id, **defaults)


def codes(result):
    return [d.code for d in result.diagnostics]


def codes_for(result, focus_id):
    return [d.code for d in result.for_focus(focus_id)]


def tree_of(*focuses, tree_id="test_tree"):
    return FocusTree(id=tree_id, tree_name=tree_id, focuses=list(focuses))


# ---------------------------------------------------------------------------
# Identity: FT001-FT004
# ---------------------------------------------------------------------------


def test_empty_focus_id_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("")))
    found = result.by_code(DiagnosticCode.FOCUS_ID_EMPTY)
    assert len(found) == 1
    assert found[0].severity is Severity.ERROR
    # Nameless focuses still get an addressable identity.
    assert found[0].object_id == "<focus #1>"


def test_focus_id_with_illegal_characters_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("bad id!")))
    found = result.by_code(DiagnosticCode.FOCUS_ID_INVALID)
    assert len(found) == 1
    assert found[0].fix_id == "sanitize-focus-id"


def test_duplicate_focus_ids_are_an_error_on_the_later_focus():
    result = validate_focus_tree(tree_of(make_focus("dup"), make_focus("dup")))
    found = result.by_code(DiagnosticCode.FOCUS_ID_DUPLICATE)
    # Reported once, against the second occurrence -- the first is the one
    # every existing reference already means.
    assert len(found) == 1
    assert "position 1" in found[0].message


def test_invalid_tree_id_is_an_error():
    tree = tree_of(make_focus("a"), tree_id="not a valid id")
    result = validate_focus_tree(tree)
    assert DiagnosticCode.TREE_ID_INVALID in codes(result)


# ---------------------------------------------------------------------------
# Prerequisites: FT010-FT013
# ---------------------------------------------------------------------------


def test_prerequisite_pointing_at_a_missing_focus_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", prerequisites=["ghost"])))
    found = result.by_code(DiagnosticCode.PREREQ_MISSING)
    assert len(found) == 1
    assert found[0].field_path == "focuses[a].prerequisites[0]"
    assert found[0].fix_id == "remove-prerequisite"


def test_self_prerequisite_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", prerequisites=["a"])))
    assert DiagnosticCode.PREREQ_SELF in codes(result)


def test_self_prerequisite_is_not_also_reported_as_a_cycle():
    """One precise finding beats two overlapping ones."""
    result = validate_focus_tree(tree_of(make_focus("a", prerequisites=["a"])))
    assert DiagnosticCode.PREREQ_CYCLE not in codes(result)


def test_duplicate_prerequisite_is_a_warning():
    result = validate_focus_tree(
        tree_of(make_focus("a"), make_focus("b", prerequisites=["a", "a"]))
    )
    found = result.by_code(DiagnosticCode.PREREQ_DUPLICATE)
    assert len(found) == 1
    assert found[0].severity is Severity.WARNING


def test_prerequisite_cycle_is_reported_against_every_member():
    tree = tree_of(
        make_focus("a", prerequisites=["c"]),
        make_focus("b", prerequisites=["a"]),
        make_focus("c", prerequisites=["b"]),
    )
    found = validate_focus_tree(tree).by_code(DiagnosticCode.PREREQ_CYCLE)
    assert {d.object_id for d in found} == {"a", "b", "c"}
    # The message names the actual loop, in the direction the edges run, so
    # the user knows which prerequisite to remove.
    assert "a requires c requires b requires a" in found[0].message


def test_long_prerequisite_chain_does_not_exhaust_the_stack():
    """Cycle detection is iterative; a deep chain must not blow up."""
    focuses = [make_focus("f0")]
    focuses += [make_focus(f"f{i}", prerequisites=[f"f{i - 1}"]) for i in range(1, 3000)]
    result = validate_focus_tree(tree_of(*focuses))
    assert not result.by_code(DiagnosticCode.PREREQ_CYCLE)


# ---------------------------------------------------------------------------
# Mutual exclusion: FT014-FT017
# ---------------------------------------------------------------------------


def test_mutually_exclusive_reference_to_missing_focus_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", mutually_exclusive=["ghost"])))
    assert DiagnosticCode.MUTEX_MISSING in codes(result)


def test_self_mutual_exclusion_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", mutually_exclusive=["a"])))
    assert DiagnosticCode.MUTEX_SELF in codes(result)


def test_duplicate_mutual_exclusion_is_a_warning():
    tree = tree_of(
        make_focus("a", mutually_exclusive=["b", "b"]),
        make_focus("b", mutually_exclusive=["a"]),
    )
    assert DiagnosticCode.MUTEX_DUPLICATE in codes(validate_focus_tree(tree))


def test_one_sided_mutual_exclusion_is_a_warning():
    tree = tree_of(make_focus("a", mutually_exclusive=["b"]), make_focus("b"))
    found = validate_focus_tree(tree).by_code(DiagnosticCode.MUTEX_ASYMMETRIC)
    assert len(found) == 1
    assert found[0].object_id == "a"
    assert found[0].fix_id == "add-reciprocal-mutually-exclusive"


def test_reciprocal_mutual_exclusion_is_clean():
    tree = tree_of(
        make_focus("a", mutually_exclusive=["b"]),
        make_focus("b", mutually_exclusive=["a"]),
    )
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.MUTEX_ASYMMETRIC)


# ---------------------------------------------------------------------------
# Relative positioning: FT018-FT020
# ---------------------------------------------------------------------------


def test_relative_position_to_missing_focus_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", relative_position_id="ghost")))
    found = result.by_code(DiagnosticCode.RELATIVE_POSITION_MISSING)
    assert len(found) == 1
    assert found[0].fix_id == "clear-relative-position"


def test_relative_position_to_self_is_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", relative_position_id="a")))
    assert DiagnosticCode.RELATIVE_POSITION_SELF in codes(result)


def test_relative_position_cycle_is_an_error():
    tree = tree_of(
        make_focus("a", relative_position_id="b"),
        make_focus("b", relative_position_id="a"),
    )
    found = validate_focus_tree(tree).by_code(DiagnosticCode.RELATIVE_POSITION_CYCLE)
    assert {d.object_id for d in found} == {"a", "b"}


# ---------------------------------------------------------------------------
# Geometry: FT030-FT031
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "3", None])
def test_non_finite_coordinate_is_an_error(bad):
    result = validate_focus_tree(tree_of(make_focus("a", x=bad)))
    found = result.by_code(DiagnosticCode.COORDINATE_INVALID)
    assert len(found) == 1
    assert found[0].field_path == "focuses[a].x"


def test_exact_coordinate_collision_is_a_warning_on_each_focus():
    tree = tree_of(make_focus("a", x=3, y=4), make_focus("b", x=3, y=4))
    found = validate_focus_tree(tree).by_code(DiagnosticCode.COORDINATE_COLLISION)
    assert {d.object_id for d in found} == {"a", "b"}


def test_near_miss_coordinates_are_not_a_collision():
    tree = tree_of(make_focus("a", x=3, y=4), make_focus("b", x=3, y=5))
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.COORDINATE_COLLISION)


def test_same_offset_under_different_anchors_is_not_a_collision():
    """Relative coordinates are only comparable within the same anchor."""
    tree = tree_of(
        make_focus("root_a"),
        make_focus("root_b", x=10),
        make_focus("a", x=1, y=1, relative_position_id="root_a"),
        make_focus("b", x=1, y=1, relative_position_id="root_b"),
    )
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.COORDINATE_COLLISION)


# ---------------------------------------------------------------------------
# Cost: FT040-FT041
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "ten", None, -5])
def test_invalid_cost_is_an_error(bad):
    result = validate_focus_tree(tree_of(make_focus("a", cost=bad)))
    assert DiagnosticCode.COST_INVALID in codes(result)


def test_zero_cost_is_a_warning_not_an_error():
    result = validate_focus_tree(tree_of(make_focus("a", cost=0)))
    found = result.by_code(DiagnosticCode.COST_ZERO)
    assert len(found) == 1
    assert found[0].severity is Severity.WARNING
    assert not result.blocks_export


def test_boolean_cost_is_rejected_rather_than_read_as_one():
    result = validate_focus_tree(tree_of(make_focus("a", cost=True)))
    assert DiagnosticCode.COST_INVALID in codes(result)


# ---------------------------------------------------------------------------
# Icons and localization: FT050-FT054
# ---------------------------------------------------------------------------


def test_missing_icon_is_a_warning():
    result = validate_focus_tree(tree_of(make_focus("a", icon=None)))
    found = result.by_code(DiagnosticCode.ICON_MISSING)
    assert len(found) == 1
    assert found[0].severity is Severity.WARNING


def test_icon_with_path_separators_is_a_warning_matching_the_exporter():
    """The exporter warns and skips such icons rather than failing."""
    result = validate_focus_tree(tree_of(make_focus("a", icon="sub/dir/icon.png")))
    found = result.by_code(DiagnosticCode.ICON_INVALID)
    assert len(found) == 1
    assert found[0].severity is Severity.WARNING
    assert not result.blocks_export


def test_unresolvable_icon_is_only_checked_when_the_context_asks():
    tree = tree_of(make_focus("a", icon="nowhere.png"))

    # Default: no filesystem access, so no finding.
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.ICON_UNRESOLVED)

    ctx = ValidationContext(check_icon_files=True, icon_search_dirs=(str(REPO_ROOT),))
    assert validate_focus_tree(tree, ctx).by_code(DiagnosticCode.ICON_UNRESOLVED)


def test_icon_present_in_the_library_resolves():
    tree = tree_of(make_focus("a", icon="known.png"))
    ctx = ValidationContext(check_icon_files=True, icon_library={"known.png": "/tmp/known.png"})
    assert not validate_focus_tree(tree, ctx).by_code(DiagnosticCode.ICON_UNRESOLVED)


def test_gfx_sprite_reference_is_never_reported_as_unresolved():
    """A GFX_ name refers to a sprite the mod does not have to ship."""
    tree = tree_of(make_focus("a", icon="GFX_focus_generic_industry"))
    ctx = ValidationContext(check_icon_files=True)
    assert not validate_focus_tree(tree, ctx).by_code(DiagnosticCode.ICON_UNRESOLVED)


def test_missing_name_is_a_warning():
    result = validate_focus_tree(tree_of(make_focus("a", name="")))
    assert DiagnosticCode.LOCALIZATION_MISSING_NAME in codes(result)


def test_name_supplied_by_external_localization_is_not_reported():
    tree = tree_of(make_focus("a", name=""))
    ctx = ValidationContext(known_localization_keys={"a"})
    assert not validate_focus_tree(tree, ctx).by_code(
        DiagnosticCode.LOCALIZATION_MISSING_NAME
    )


def test_missing_description_is_informational_only():
    result = validate_focus_tree(tree_of(make_focus("a", description="")))
    found = result.by_code(DiagnosticCode.LOCALIZATION_MISSING_DESCRIPTION)
    assert len(found) == 1
    assert found[0].severity is Severity.INFO
    assert not result.blocks_export


# ---------------------------------------------------------------------------
# Structure: FT060-FT061
# ---------------------------------------------------------------------------


def test_focus_behind_a_missing_prerequisite_is_unreachable():
    tree = tree_of(make_focus("a", prerequisites=["ghost"]))
    found = validate_focus_tree(tree).by_code(DiagnosticCode.FOCUS_UNREACHABLE)
    assert [d.object_id for d in found] == ["a"]


def test_focus_reachable_through_a_chain_is_not_reported():
    tree = tree_of(
        make_focus("root"),
        make_focus("mid", prerequisites=["root"]),
        make_focus("leaf", prerequisites=["mid"]),
    )
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.FOCUS_UNREACHABLE)


def test_ungrouped_prerequisites_are_all_required():
    """Separate prerequisite blocks are AND-ed, so one missing breaks reach."""
    tree = tree_of(
        make_focus("root"),
        make_focus("leaf", prerequisites=["root", "ghost"]),
    )
    found = validate_focus_tree(tree).by_code(DiagnosticCode.FOCUS_UNREACHABLE)
    assert [d.object_id for d in found] == ["leaf"]


def test_grouped_prerequisites_only_need_one_satisfied():
    """A single block is an OR, so one reachable member is enough."""
    tree = tree_of(
        make_focus("root"),
        make_focus("leaf", prerequisites=["root", "ghost"], prerequisites_grouped=True),
    )
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.FOCUS_UNREACHABLE)


def test_tree_where_everything_has_a_prerequisite_has_no_root():
    tree = tree_of(
        make_focus("a", prerequisites=["b"]),
        make_focus("b", prerequisites=["a"]),
    )
    found = validate_focus_tree(tree).by_code(DiagnosticCode.TREE_HAS_NO_ROOT)
    assert len(found) == 1
    assert found[0].object_id is None


def test_an_empty_tree_has_no_findings_at_all():
    result = validate_focus_tree(tree_of())
    assert result.diagnostics == []
    assert not result.blocks_export


# ---------------------------------------------------------------------------
# Export safety: FT070-FT072
# ---------------------------------------------------------------------------


def test_icons_differing_only_by_extension_collide_into_one_sprite():
    """flag.png and flag.dds both export as the sprite 'flag'."""
    tree = tree_of(
        make_focus("a", icon="flag.png"),
        make_focus("b", icon="flag.dds"),
    )
    found = validate_focus_tree(tree).by_code(DiagnosticCode.EXPORT_FILENAME_COLLISION)
    assert {d.object_id for d in found} == {"a", "b"}
    assert found[0].severity is Severity.ERROR
    assert "Only one of them survives" in found[0].message


def test_icons_whose_names_slugify_the_same_collide():
    tree = tree_of(
        make_focus("a", icon="my icon.png"),
        make_focus("b", icon="my_icon.png"),
    )
    assert validate_focus_tree(tree).by_code(DiagnosticCode.EXPORT_FILENAME_COLLISION)


def test_the_same_icon_on_two_focuses_is_not_a_collision():
    """Sharing one icon is normal; it is one file, exported once."""
    tree = tree_of(make_focus("a", icon="icon.png"), make_focus("b", icon="icon.png"))
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.EXPORT_FILENAME_COLLISION)


def test_distinct_icon_names_do_not_collide():
    tree = tree_of(make_focus("a", icon="alpha.png"), make_focus("b", icon="beta.png"))
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.EXPORT_FILENAME_COLLISION)


def test_gfx_sprite_references_never_collide():
    """They name existing sprites; the exporter derives and copies nothing."""
    tree = tree_of(
        make_focus("a", icon="GFX_shared_sprite"),
        make_focus("b", icon="GFX_shared_sprite"),
    )
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.EXPORT_FILENAME_COLLISION)


def test_slug_matches_the_exporter():
    """The rule predicts collisions only if it slugifies identically."""
    from _exporter import HOI4Exporter
    from _validation import _slugify_like_exporter

    exporter = HOI4Exporter()
    for value in ("flag", "my icon", "my_icon", "a-b", "Ünicode", "  spaced  ", "__x__"):
        assert _slugify_like_exporter(value, value) == exporter._slugify(value, value)


def test_unbalanced_preserved_fragment_is_a_warning():
    tree = tree_of(make_focus("a", raw_unparsed=["custom_block = { unclosed = yes"]))
    found = validate_focus_tree(tree).by_code(DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE)
    assert len(found) == 1
    assert "unclosed block" in found[0].message


def test_unterminated_string_in_a_preserved_fragment_is_a_warning():
    tree = tree_of(make_focus("a", raw_unparsed=['custom = "no closing quote']))
    found = validate_focus_tree(tree).by_code(DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE)
    assert "unterminated quoted string" in found[0].message


def test_balanced_preserved_fragment_is_accepted():
    tree = tree_of(make_focus("a", raw_unparsed=["custom_block = { value = yes }"]))
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE)


def test_braces_inside_comments_and_strings_do_not_unbalance_a_fragment():
    fragment = 'custom = { text = "a { brace" } # trailing { comment'
    tree = tree_of(make_focus("a", raw_unparsed=[fragment]))
    assert not validate_focus_tree(tree).by_code(DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE)


def test_stray_closing_brace_is_reported():
    tree = tree_of(make_focus("a", raw_unparsed=["} orphan"]))
    found = validate_focus_tree(tree).by_code(DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE)
    assert "no matching opening brace" in found[0].message


# ---------------------------------------------------------------------------
# Subsystem properties
# ---------------------------------------------------------------------------


def test_a_clean_tree_produces_nothing():
    tree = tree_of(make_focus("root"), make_focus("child", prerequisites=["root"], x=1))
    assert validate_focus_tree(tree).diagnostics == []


def test_results_are_deterministic_across_runs():
    tree = tree_of(
        make_focus("z", prerequisites=["ghost"], icon=None),
        make_focus("a", cost=-1, x=0, y=0),
        make_focus("m", x=0, y=0, mutually_exclusive=["a"]),
    )
    first = [d.to_dict() for d in validate_focus_tree(tree).diagnostics]
    second = [d.to_dict() for d in validate_focus_tree(tree).diagnostics]
    assert first == second
    assert len(first) > 3


def test_results_are_ordered_errors_then_warnings_then_info():
    tree = tree_of(make_focus("a", prerequisites=["ghost"], icon=None, description=""))
    severities = [d.severity for d in validate_focus_tree(tree).diagnostics]
    assert severities == sorted(severities, key=lambda s: s.rank)


def test_repeated_validation_does_not_accumulate_results():
    """Each run builds its result from scratch; nothing is carried over."""
    tree = tree_of(make_focus("a", prerequisites=["ghost"]))
    sizes = {len(validate_focus_tree(tree)) for _ in range(5)}
    assert len(sizes) == 1


def test_only_errors_block_export():
    tree = tree_of(make_focus("a", icon=None, description="", cost=0))
    result = validate_focus_tree(tree)
    assert result.warnings and result.infos
    assert not result.errors
    assert not result.blocks_export

    blocked = validate_focus_tree(tree_of(make_focus("a", prerequisites=["ghost"])))
    assert blocked.blocks_export


@pytest.mark.parametrize(
    "tree",
    [
        pytest.param(
            FocusTree(id="bad tree id", focuses=[make_focus("a")]),
            id="invalid-tree-id",
        ),
        pytest.param(
            FocusTree(id="ok", focuses=[make_focus("bad focus id")]),
            id="invalid-focus-id",
        ),
        pytest.param(FocusTree(id="ok", focuses=[make_focus("")]), id="empty-focus-id"),
    ],
)
def test_everything_the_exporter_refuses_to_write_is_an_error(tree, tmp_path):
    """Acceptance: every export-blocking condition has a blocking diagnostic.

    Rather than trusting a hand-maintained list, this drives the real
    exporter and asserts that whatever it records as a validation failure
    the validator also flags as an error.
    """
    from _exporter import HOI4Exporter

    report, _path, _summary = HOI4Exporter().export(tree, str(tmp_path))
    assert report.validation_failures, "fixture should be rejected by the exporter"
    assert validate_focus_tree(tree).blocks_export


def test_every_emitted_code_is_documented_in_the_catalog():
    tree = tree_of(
        make_focus("", prerequisites=["ghost"]),
        make_focus("bad id"),
        make_focus("dup"),
        make_focus("dup"),
        make_focus("a", prerequisites=["a"], mutually_exclusive=["a"], cost=-1, x=float("nan")),
        make_focus("b", relative_position_id="b", icon="x/y.png", name="", description=""),
        make_focus("c", prerequisites=["d"], mutually_exclusive=["e"], cost=0, icon=None),
        make_focus("d", prerequisites=["c"], raw_unparsed=["{"]),
        make_focus("e", x=0, y=0),
        make_focus("f", x=0, y=0),
        tree_id="bad tree id",
    )
    emitted = set(codes(validate_focus_tree(tree)))
    assert emitted, "fixture should trip many rules"
    assert emitted <= set(DIAGNOSTIC_CATALOG)


def test_catalog_severity_matches_what_the_rules_emit():
    tree = tree_of(
        make_focus("a", prerequisites=["ghost"], icon=None, description="", cost=0),
        make_focus("b", mutually_exclusive=["a"]),
    )
    for diagnostic in validate_focus_tree(tree):
        assert diagnostic.severity is DIAGNOSTIC_CATALOG[diagnostic.code].severity


def test_catalog_covers_every_declared_code_and_nothing_more():
    declared = {
        getattr(DiagnosticCode, name)
        for name in dir(DiagnosticCode)
        if name.isupper() and not name.startswith("_")
    }
    assert declared == set(DIAGNOSTIC_CATALOG)


def test_codes_are_unique():
    declared = [
        getattr(DiagnosticCode, name)
        for name in dir(DiagnosticCode)
        if name.isupper() and not name.startswith("_")
    ]
    assert len(declared) == len(set(declared))


def test_validator_accepts_a_bare_list_of_focuses():
    """Useful from a script that never builds a full tree."""
    result = validate_focus_tree([make_focus("a", prerequisites=["ghost"])])
    assert DiagnosticCode.PREREQ_MISSING in codes(result)


def test_source_locations_are_carried_onto_diagnostics():
    ctx = ValidationContext(
        source_locations={"a": SourceLocation("germany.txt", 42, 7)}
    )
    result = validate_focus_tree(tree_of(make_focus("a", prerequisites=["ghost"])), ctx)
    diagnostic = result.by_code(DiagnosticCode.PREREQ_MISSING)[0]
    assert str(diagnostic.location) == "germany.txt:42:7"
    assert "germany.txt:42:7" in diagnostic.format()


def test_diagnostics_carry_every_required_field():
    result = validate_focus_tree(tree_of(make_focus("a", prerequisites=["ghost"])))
    diagnostic = result.by_code(DiagnosticCode.PREREQ_MISSING)[0]
    assert diagnostic.code
    assert isinstance(diagnostic.severity, Severity)
    assert diagnostic.message
    assert diagnostic.object_id == "a"
    assert diagnostic.field_path
    assert diagnostic.suggestion
    assert diagnostic.fix_id  # this one is machine-fixable
    assert diagnostic.to_dict()["severity"] == "error"


def test_diagnostics_are_immutable_values():
    diagnostic = Diagnostic(code="FT001", severity=Severity.ERROR, message="x")
    with pytest.raises(Exception):
        diagnostic.code = "FT002"


def test_validation_is_useful_without_any_gui():
    """The whole point of a pure validator: it runs where Qt does not.

    Imported in a fresh interpreter with PyQt6 poisoned, so an accidental
    Qt dependency anywhere in the module's import graph fails this test.
    """
    program = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "class _Blocked:\n"
        "    def __getattr__(self, name):\n"
        "        raise AssertionError('_validation must not import PyQt6')\n"
        "    def find_module(self, *a, **k):\n"
        "        return None\n"
        "for name in list(sys.modules):\n"
        "    if name.startswith('PyQt6'):\n"
        "        del sys.modules[name]\n"
        "sys.modules['PyQt6'] = _Blocked()\n"
        "from _dataStructs import Focus, FocusTree\n"
        "from _validation import validate_focus_tree\n"
        "tree = FocusTree(id='t', focuses=[Focus(id='a', prerequisites=['ghost'])])\n"
        "result = validate_focus_tree(tree)\n"
        "assert result.blocks_export\n"
        "print(result.format())\n"
    ) % str(REPO_ROOT / "source")

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    assert "FT010" in completed.stdout


def test_large_tree_validates_in_reasonable_time():
    """A tree far larger than any real mod must still complete promptly."""
    focuses = [make_focus("root", x=0, y=0)]
    focuses += [
        make_focus(f"f{i}", prerequisites=[f"f{i - 1}" if i > 1 else "root"], x=i, y=i)
        for i in range(1, 2000)
    ]
    result = validate_focus_tree(tree_of(*focuses))
    assert result.diagnostics == []
