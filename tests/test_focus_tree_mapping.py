"""Tests for the semantic mapper (source/_txt_converter.py).

The mapper's job is to pull focus-tree fields out of the concrete syntax tree
*without* consuming it: unknown blocks stay in ``raw``, the CST stays reachable
on the parser, and structurally invalid input surfaces as diagnostics rather
than a silently truncated tree.

Pure Python, no Qt, no GUI construction.
"""

from pathlib import Path

import pytest

from _paradox_cst import Block
from _txt_converter import ParadoxSyntaxError, TxtFocusParser, convert_txt_to_project_dict

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "paradox"
VALID_DIR = FIXTURES / "valid"
INVALID_DIR = FIXTURES / "invalid"

VALID_FIXTURES = sorted(VALID_DIR.glob("*.txt"))
INVALID_FIXTURES = sorted(INVALID_DIR.glob("*.txt"))


def _convert(name: str):
    path = VALID_DIR / name
    return convert_txt_to_project_dict(path.read_text(encoding="utf-8"), filename=name)


def _by_id(result):
    return {focus["id"]: focus for focus in result["focuses"]}


# ---------------------------------------------------------------------------
# Clean corpus
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixtures_map_without_diagnostics(path):
    result = convert_txt_to_project_dict(
        path.read_text(encoding="utf-8"), filename=path.name
    )
    assert result["diagnostics"] == []
    assert result["focuses"]


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_mapping_is_deterministic(path):
    text = path.read_text(encoding="utf-8")
    first = convert_txt_to_project_dict(text, filename=path.name)
    second = convert_txt_to_project_dict(text, filename=path.name)
    assert first == second


def test_tree_info_maps_from_nested_country_modifiers():
    result = _convert("vanilla_style.txt")
    tree = result["tree_info"]
    assert tree["id"] == "vanilla_style_tree"
    assert tree["country"] == ["ENG"]
    assert tree["default"] is False
    assert tree["reset_on_civil_war"] is False
    assert tree["continuous_focus_position"] == {"x": 50, "y": 1200}
    # `initial_show_position = { focus = ... }` has no coordinates.
    assert tree["initial_show_position"] is None


def test_scalar_shared_focus_is_a_tree_pointer_not_a_focus():
    result = convert_txt_to_project_dict(
        "focus_tree = { id = t shared_focus = other_tree_focus "
        "focus = { id = a x = 0 y = 0 } }"
    )
    assert result["tree_info"]["shared_focus"] == "other_tree_focus"
    assert list(_by_id(result)) == ["a"]


def test_shared_focus_block_is_treated_as_a_focus_definition():
    result = convert_txt_to_project_dict(
        "shared_focus = { id = shared_one x = 1 y = 2 cost = 4 }"
    )
    focus = _by_id(result)["shared_one"]
    assert (focus["x"], focus["y"], focus["cost"]) == (1, 2, 4)


def test_relative_positions_resolve_transitively():
    result = _by_id(_convert("vanilla_style.txt"))
    # root at (4, 0); army is (-2, 1) relative to root; join_war is (2, 1)
    # relative to army -- so the chain has to be followed, not just one hop.
    assert (result["vanilla_root"]["x"], result["vanilla_root"]["y"]) == (4, 0)
    assert (result["vanilla_army"]["x"], result["vanilla_army"]["y"]) == (2, 1)
    assert (result["vanilla_join_war"]["x"], result["vanilla_join_war"]["y"]) == (4, 2)
    assert result["vanilla_join_war"]["_relative_to"] == "vanilla_army"
    assert result["vanilla_join_war"]["_relative_offset_x"] == 2


def test_dangling_relative_position_is_reported_not_silently_zeroed():
    result = convert_txt_to_project_dict(
        "focus = { id = a x = 5 y = 6 relative_position_id = nowhere }",
        filename="rel.txt",
    )
    focus = _by_id(result)["a"]
    assert (focus["x"], focus["y"]) == (5, 6)
    codes = [d["code"] for d in result["diagnostics"]]
    assert "unknown-relative-position" in codes


def test_circular_relative_positions_terminate_with_a_diagnostic():
    result = convert_txt_to_project_dict(
        "focus = { id = a x = 1 y = 1 relative_position_id = b }"
        "focus = { id = b x = 2 y = 2 relative_position_id = a }",
        filename="cycle.txt",
    )
    codes = [d["code"] for d in result["diagnostics"]]
    assert "circular-relative-position" in codes
    assert set(_by_id(result)) == {"a", "b"}


# ---------------------------------------------------------------------------
# Duplicates and repeated keys
# ---------------------------------------------------------------------------

def test_repeated_prerequisite_blocks_stay_separate_groups_in_order():
    focus = _by_id(_convert("duplicate_keys.txt"))["duplicate_root"]
    assert focus["prerequisites_groups"] == [
        {"type": "AND", "items": ["dup_a"]},
        {"type": "OR", "items": ["dup_b", "dup_c"]},
        {"type": "AND", "items": ["dup_d"]},
    ]
    assert focus["prerequisites"] == ["dup_a", "dup_b", "dup_c", "dup_d"]


def test_repeated_search_filters_and_country_tags_accumulate_in_order():
    result = _convert("duplicate_keys.txt")
    assert result["tree_info"]["country"] == ["SOV", "RAJ"]
    focus = _by_id(result)["duplicate_root"]
    assert focus["search_filters"] == [
        "FOCUS_FILTER_POLITICAL",
        "FOCUS_FILTER_INDUSTRY",
    ]


def test_repeated_block_field_becomes_a_list_in_source_order():
    focus = _by_id(_convert("duplicate_keys.txt"))["duplicate_root"]
    rewards = focus["completion_reward"]
    assert isinstance(rewards, list) and len(rewards) == 2
    assert "add_political_power" in rewards[0]
    assert "add_stability" in rewards[1]


def test_conditional_icons_keep_their_triggers_and_order():
    focus = _by_id(_convert("duplicate_keys.txt"))["duplicate_root"]
    icons = focus["icon"]
    assert [entry["value"] for entry in icons] == [
        "GFX_goal_generic_fascism",
        "GFX_goal_generic_communism",
        "GFX_goal_generic_fallback",
    ]
    assert "has_government = fascism" in icons[0]["trigger"]
    assert icons[2]["trigger"] is None


def test_single_unconditional_icon_stays_a_plain_string():
    focus = _by_id(_convert("vanilla_style.txt"))["vanilla_root"]
    assert focus["icon"] == "GFX_goal_generic_political_pressure"


# ---------------------------------------------------------------------------
# Comments and quoted braces
# ---------------------------------------------------------------------------

def test_commented_out_block_is_not_mapped():
    focus = _by_id(_convert("comments_and_quoted_braces.txt"))["comment_focus"]
    # The fixture's only `available` block is inside a comment.
    assert focus["available"] is None


def test_quoted_braces_and_escapes_survive_into_field_values():
    focus = _by_id(_convert("comments_and_quoted_braces.txt"))["comment_focus"]
    assert focus["name"] == 'a name with { braces } and a "quoted" word'
    assert focus["icon"] == "GFX_with_a_}_in_the_name"
    assert "closing brace } inside a string" in focus["completion_reward"]


# ---------------------------------------------------------------------------
# Nested logical expressions
# ---------------------------------------------------------------------------

def test_or_group_inside_a_prerequisite_keeps_its_kind():
    focus = _by_id(_convert("nested_logic.txt"))["logic_child"]
    assert focus["prerequisites_groups"] == [
        {"type": "OR", "items": ["logic_root", "logic_alt"]}
    ]


def test_nested_logic_is_kept_verbatim_in_the_available_block():
    focus = _by_id(_convert("nested_logic.txt"))["logic_child"]
    available = focus["available"]
    assert "NOT" in available and "has_dlc" in available
    # Deeply nested conditions are still discoverable for the GUI simulation.
    kinds = {(c["type"], c["value"]) for c in focus["avail_conditions"]}
    assert ("has_government", "fascism") in kinds
    assert ("has_dlc", "Waking the Tiger") in kinds


def test_trivial_always_yes_visible_block_does_not_hide_a_focus():
    focus = _by_id(_convert("nested_logic.txt"))["logic_child"]
    assert focus["hidden"] is False
    assert focus["hidden_tags"] == []


def test_constraining_visible_block_hides_and_tags_the_focus():
    focus = _by_id(_convert("vanilla_style.txt"))["vanilla_join_war"]
    assert focus["hidden"] is True
    assert focus["hidden_tags"] == ["government:democratic"]


# ---------------------------------------------------------------------------
# Unknown constructs
# ---------------------------------------------------------------------------

def test_unknown_blocks_are_preserved_in_raw_and_listed_separately():
    focus = _by_id(_convert("unknown_mod_constructs.txt"))["modded_focus"]
    assert focus["has_unparsed"] is True
    assert len(focus["raw_unparsed"]) == 2
    assert any("custom_mod_effect" in block for block in focus["raw_unparsed"])
    assert any("dynamic_modifier_block" in block for block in focus["raw_unparsed"])
    # Nothing was discarded: raw still contains them verbatim.
    for block in focus["raw_unparsed"]:
        assert block in focus["raw"]


def test_clean_raw_drops_only_the_unknown_blocks():
    focus = _by_id(_convert("unknown_mod_constructs.txt"))["modded_focus"]
    clean = focus["clean_raw"]
    assert "custom_mod_effect" not in clean
    assert "dynamic_modifier_block" not in clean
    # Known content around them is untouched.
    assert "completion_reward" in clean
    assert "has_completed_focus = modded_prereq" in clean


def test_nested_logic_inside_a_known_block_is_not_flagged_as_unknown():
    """`OR` inside `available` is part of a construct we model, not an unknown."""
    focus = _by_id(_convert("nested_logic.txt"))["logic_child"]
    assert focus["has_unparsed"] is False
    assert focus["raw_unparsed"] == []


def test_unknown_tree_level_blocks_survive_on_the_syntax_tree():
    path = VALID_DIR / "unknown_mod_constructs.txt"
    parser = TxtFocusParser(path.read_text(encoding="utf-8"), filename=path.name)
    parser.parse()

    tree = parser.document.block("focus_tree")
    metadata = tree.block("kaiserreich_metadata")
    assert metadata is not None
    assert metadata.scalar("branch").value == "syndicalist"
    # Anonymous values in an unknown block are kept too.
    assert tree.block("editor_hints").value_strings() == ["collapsed", "pinned"]


def test_syntax_tree_stays_available_alongside_the_mapped_dicts():
    path = VALID_DIR / "vanilla_style.txt"
    parser = TxtFocusParser(path.read_text(encoding="utf-8"), filename=path.name)
    result = parser.parse()

    assert parser.document is not None
    assert len(parser.focus_nodes) == len(result["focuses"])
    for node, focus in zip(parser.focus_nodes, result["focuses"]):
        assert isinstance(node, Block)
        assert node.scalar("id").value == focus["id"]


# ---------------------------------------------------------------------------
# Unusual spacing
# ---------------------------------------------------------------------------

def test_spacing_never_changes_the_mapping():
    result = _by_id(_convert("unusual_spacing.txt"))
    assert set(result) == {"spacing_tight", "spacing_loose", "spacing_jammed"}
    assert (result["spacing_loose"]["x"], result["spacing_loose"]["y"]) == (3, 4)
    assert result["spacing_jammed"]["prerequisites"] == ["spacing_tight"]


# ---------------------------------------------------------------------------
# Invalid input reaches callers as structured diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_invalid_input_yields_serializable_diagnostics(path):
    result = convert_txt_to_project_dict(
        path.read_text(encoding="utf-8"), filename=path.name
    )
    diagnostics = result["diagnostics"]
    assert diagnostics
    for record in diagnostics:
        assert isinstance(record, dict)
        assert set(record) == {
            "severity", "code", "message", "filename", "line", "column",
            "offset", "token", "expected", "context",
        }
        assert record["filename"] == path.name


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_strict_mode_refuses_structurally_invalid_input(path):
    with pytest.raises(ParadoxSyntaxError):
        convert_txt_to_project_dict(
            path.read_text(encoding="utf-8"), filename=path.name, recover=False
        )


def test_recovery_still_imports_the_focuses_it_could_read():
    path = INVALID_DIR / "unclosed_block.txt"
    result = convert_txt_to_project_dict(
        path.read_text(encoding="utf-8"), filename=path.name
    )
    assert result["diagnostics"]
    assert list(_by_id(result)) == ["unclosed_focus"]


def test_focus_without_id_or_name_is_reported_not_dropped_silently():
    result = convert_txt_to_project_dict(
        "focus_tree = { id = t focus = { x = 1 y = 2 } }", filename="anon.txt"
    )
    assert result["focuses"] == []
    codes = [d["code"] for d in result["diagnostics"]]
    assert "focus-without-id" in codes
