"""Smoke tests for the Paradox .txt focus-tree import path
(source/_txt_converter.py: convert_txt_to_project_dict), covering
prerequisites, mutual exclusivity, and hidden/visible-block detection.

This is a pure-Python parser with no Qt dependency, so it is tested
directly without constructing any GUI.
"""

from pathlib import Path

from _txt_converter import convert_txt_to_project_dict

FIXTURE_TXT = Path(__file__).resolve().parent / "fixtures" / "sample_focus_tree.txt"


def _load_result():
    txt_content = FIXTURE_TXT.read_text(encoding="utf-8")
    return convert_txt_to_project_dict(txt_content)


def test_tree_info_parsed():
    result = _load_result()
    assert result["tree_info"]["id"] == "sample_focus_tree"
    assert "GER" in result["tree_info"]["country"]


def test_all_focuses_parsed():
    result = _load_result()
    ids = {f["id"] for f in result["focuses"]}
    assert ids == {
        "sample_root",
        "sample_army_path",
        "sample_navy_path",
        "sample_hidden_focus",
    }


def test_prerequisites_parsed():
    result = _load_result()
    by_id = {f["id"]: f for f in result["focuses"]}
    assert by_id["sample_army_path"]["prerequisites"] == ["sample_root"]
    assert by_id["sample_navy_path"]["prerequisites"] == ["sample_root"]
    assert set(by_id["sample_hidden_focus"]["prerequisites"]) == {
        "sample_army_path",
        "sample_navy_path",
    }


def test_mutually_exclusive_parsed():
    result = _load_result()
    by_id = {f["id"]: f for f in result["focuses"]}
    assert by_id["sample_army_path"]["mutually_exclusive"] == ["sample_navy_path"]
    assert by_id["sample_navy_path"]["mutually_exclusive"] == ["sample_army_path"]


def test_hidden_focus_detected_with_reveal_tag():
    result = _load_result()
    by_id = {f["id"]: f for f in result["focuses"]}
    hidden_focus = by_id["sample_hidden_focus"]
    assert hidden_focus["hidden"] is True
    assert "government:fascism" in hidden_focus["hidden_tags"]

    # Non-hidden focuses should not be flagged.
    assert by_id["sample_root"]["hidden"] is False
    assert by_id["sample_army_path"]["hidden"] is False


def test_positions_parsed():
    result = _load_result()
    by_id = {f["id"]: f for f in result["focuses"]}
    assert (by_id["sample_root"]["x"], by_id["sample_root"]["y"]) == (0, 0)
    assert (by_id["sample_army_path"]["x"], by_id["sample_army_path"]["y"]) == (-2, 1)
    assert (by_id["sample_navy_path"]["x"], by_id["sample_navy_path"]["y"]) == (2, 1)


def test_result_dict_is_a_valid_project_focuses_source():
    # convert_txt_to_project_dict's output feeds straight into the same
    # 'focuses' list shape load_project_from_dict() expects.
    result = _load_result()
    for focus in result["focuses"]:
        assert isinstance(focus["id"], str) and focus["id"]
        assert isinstance(focus["prerequisites"], list)
        assert isinstance(focus["mutually_exclusive"], list)
