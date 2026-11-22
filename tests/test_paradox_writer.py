"""Tests for the structured Paradox writer (source/_paradox_writer.py) and
the export validation/reporting layer (source/_export_validation.py) built
on top of it.

Covered here:
* Centralized string escaping (quotes, backslashes, control chars, Unicode).
* Quoting decisions driven by content, not a spaces-only heuristic.
* Deterministic rendering (repeat calls, byte-identical output).
* A golden-file check for stable focus-tree formatting.
* Semantic round-trips through the real lexer/parser corpus: parse a
  fixture, convert it to writer nodes, render, reparse, and compare
  structure -- including unknown/mod-specific constructs and duplicate keys.
* Path-traversal and identifier/filename validation.
* End-to-end HOI4Exporter.export() producing an ExportReport, including a
  localisation file with quotes, backslashes, Unicode, newlines, and colons.
"""

from pathlib import Path

import pytest

from _dataStructs import Focus, FocusTree
from _exporter import HOI4Exporter
from _export_validation import (
    ExportValidationError,
    PathTraversalError,
    resolve_within,
    validate_filename,
    validate_focus_id,
    validate_icon_name,
    validate_localization_key,
)
from _paradox_cst import Assignment, Block as CstBlock, Scalar, parse
from _paradox_writer import (
    Assign,
    Bool,
    Ident,
    Num,
    Str,
    atom,
    block,
    escape_paradox_string,
    from_cst_document,
    inline_block,
    needs_quoting,
    render,
    value_node,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "paradox" / "valid"


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('say "hi"', 'say \\"hi\\"'),
        ("back\\slash", "back\\\\slash"),
        ("line1\nline2", "line1\\nline2"),
        ("tab\there", "tab\\there"),
        ("windows\r\n", "windows\\n"),
        ("café \u00e9\u00e8", "café \u00e9\u00e8"),
        ("a: b", "a: b"),
        ("bell\x07byte", "bellbyte"),
    ],
)
def test_escape_paradox_string_rules(raw, expected):
    assert escape_paradox_string(raw) == expected


def test_escaped_string_is_valid_script_text_for_the_real_lexer():
    """Anything the writer escapes must lex back as one clean string token.

    The lexer's own decoding only resolves ``\\"`` and ``\\\\`` (it leaves
    ``\\n``/``\\t`` as the literal two characters, matching how HOI4's
    localisation system -- not the generic script parser -- interprets
    them), so this checks the escaped form parses without diagnostics and
    that quotes/backslashes specifically survive the escape-then-decode
    cycle intact.
    """
    from _paradox_lexer import tokenize

    raw = 'quote " backslash \\ colon : unicode éè tab\t newline\n'
    text = f'"{escape_paradox_string(raw)}"'
    tokens, diagnostics = tokenize(text)
    assert diagnostics == []
    string_tokens = [t for t in tokens if t.text.startswith('"')]
    assert len(string_tokens) == 1
    decoded = string_tokens[0].value
    assert decoded.count('"') == raw.count('"')
    assert "colon : unicode éè" in decoded


def test_quoting_is_not_a_spaces_only_check():
    assert needs_quoting("no_spaces_but=equals")
    assert needs_quoting("has{brace")
    assert needs_quoting("has#hash")
    assert needs_quoting("")
    assert not needs_quoting("plain_identifier")
    assert not needs_quoting("GFX_goal_generic_army")


def test_atom_quotes_only_when_needed():
    assert atom("plain") == Ident("plain")
    assert atom("has space") == Str("has space")
    assert atom("a=b") == Str("a=b")


def test_value_node_dispatches_on_python_type():
    assert value_node(True) == Bool(True)
    assert value_node(3) == Num(3)
    assert value_node(0.5) == Num(0.5)
    assert value_node("plain") == Ident("plain")
    assert value_node("has space") == Str("has space")
    with pytest.raises(TypeError):
        value_node(object())


# ---------------------------------------------------------------------------
# Deterministic rendering / golden file
# ---------------------------------------------------------------------------

def _sample_tree() -> FocusTree:
    tree = FocusTree(id="golden_tree", tree_name="golden_tree", country_tag="GER")
    tree.focuses = [
        Focus(id="root", name="root", x=0, y=0, cost=10),
        Focus(
            id="army_path",
            name="army_path",
            x=-1,
            y=1,
            cost=7,
            icon="GFX_goal_generic_army_doctrines",
            prerequisites=["root"],
            mutually_exclusive=["navy_path"],
            search_filters=["FOCUS_FILTER_MILITARY"],
        ),
    ]
    return tree


GOLDEN_OUTPUT = (
    "focus_tree = {\n"
    "\tid = golden_tree\n"
    "\n"
    "\tcountry = {\n"
    "\t\tfactor = 0\n"
    "\t}\n"
    "\n"
    "\treset_on_civilwar = no\n"
    "\n"
    "\tfocus = {\n"
    "\t\tid = root\n"
    "\t\tx = 0\n"
    "\t\ty = 0\n"
    "\t\tcost = 10\n"
    "\t}\n"
    "\n"
    "\tfocus = {\n"
    "\t\tid = army_path\n"
    "\t\ticon = GFX_goal_generic_army_doctrines\n"
    "\t\tx = -1\n"
    "\t\ty = 1\n"
    "\t\tcost = 7\n"
    "\t\tsearch_filters = { FOCUS_FILTER_MILITARY }\n"
    "\t\tprerequisite = { focus = root }\n"
    "\t\tmutually_exclusive = { focus = navy_path }\n"
    "\t}\n"
    "\n"
    "}\n"
)


def test_golden_focus_tree_formatting():
    exporter = HOI4Exporter()
    assert exporter.tree_to_string(_sample_tree()) == GOLDEN_OUTPUT


def test_identical_input_produces_byte_identical_output():
    exporter = HOI4Exporter()
    tree = _sample_tree()
    first = exporter.tree_to_string(tree)
    second = exporter.tree_to_string(tree)
    assert first == second
    third = HOI4Exporter().tree_to_string(_sample_tree())
    assert first == third


# ---------------------------------------------------------------------------
# Semantic round-trip through the real parser corpus
# ---------------------------------------------------------------------------

def _canonical(node):
    """A hashable, order-preserving shape for a parsed node, ignoring spans."""
    if isinstance(node, Scalar):
        return ("scalar", node.value, node.is_quoted)
    if isinstance(node, CstBlock):
        return ("block", tuple(_canonical(c) for c in node.children))
    if isinstance(node, Assignment):
        return ("assign", node.key.value.lower(), node.op, _canonical(node.value))
    raise TypeError(type(node))


@pytest.mark.parametrize("fixture_path", sorted(FIXTURES.glob("*.txt")), ids=lambda p: p.name)
def test_semantic_round_trip_through_writer(fixture_path):
    original = parse(fixture_path.read_text(encoding="utf-8"), filename=str(fixture_path), recover=False)

    nodes = from_cst_document(original)
    rendered = render(nodes)

    reparsed = parse(rendered, filename="<rendered>", recover=False)

    original_shape = [_canonical(c) for c in original.children]
    reparsed_shape = [_canonical(c) for c in reparsed.children]
    assert reparsed_shape == original_shape


def test_duplicate_keys_and_unknown_constructs_survive_round_trip():
    fixture = FIXTURES / "duplicate_keys.txt"
    original = parse(fixture.read_text(encoding="utf-8"), recover=False)
    rendered = render(from_cst_document(original))
    reparsed = parse(rendered, recover=False)

    focus_tree = original.block("focus_tree")
    reexported_tree = reparsed.block("focus_tree")
    focus = [b for b in focus_tree.blocks("focus") if b.scalar("id").value == "duplicate_root"][0]
    reexported_focus = [b for b in reexported_tree.blocks("focus") if b.scalar("id").value == "duplicate_root"][0]

    # Three repeated `prerequisite` keys and two repeated `icon` keys must
    # all still be present, in the same order, after the round trip.
    assert len(focus.get_all("prerequisite")) == 3
    assert len(reexported_focus.get_all("prerequisite")) == 3
    assert [_canonical(a) for a in focus.get_all("icon")] == [
        _canonical(a) for a in reexported_focus.get_all("icon")
    ]


# ---------------------------------------------------------------------------
# Validation and path safety
# ---------------------------------------------------------------------------

def test_validate_focus_id_rejects_unsafe_characters():
    validate_focus_id("valid_id_1")
    for bad in ["", "has space", 'has"quote', "has{brace", "has=eq"]:
        with pytest.raises(ExportValidationError):
            validate_focus_id(bad)


def test_validate_filename_rejects_traversal_and_separators():
    validate_filename("focus_tree.txt")
    for bad in ["..", ".", "../escape.txt", "a/b.txt", "a\\b.txt", "con:evil.txt"]:
        with pytest.raises(ExportValidationError):
            validate_filename(bad)


def test_validate_icon_and_localization_key():
    validate_icon_name("GFX_goal_generic_army")
    with pytest.raises(ExportValidationError):
        validate_icon_name("../escape")
    validate_localization_key("focus_id_1")
    with pytest.raises(ExportValidationError):
        validate_localization_key("bad key")


def test_resolve_within_blocks_path_traversal(tmp_path):
    root = tmp_path / "mod_root"
    root.mkdir()
    resolve_within(str(root), "common", "national_focus", "tree.txt")
    with pytest.raises(PathTraversalError):
        resolve_within(str(root), "..", "escape.txt")
    with pytest.raises(PathTraversalError):
        resolve_within(str(root), "common", "..", "..", "escape.txt")


# ---------------------------------------------------------------------------
# End-to-end export report and localisation escaping
# ---------------------------------------------------------------------------

def test_export_rejects_invalid_focus_id_without_writing(tmp_path):
    tree = FocusTree(id="bad tree id", tree_name="bad")
    exporter = HOI4Exporter()

    ok = exporter.export_tree(tree, str(tmp_path), write_localisation=False, write_gfx=False)

    assert ok is False
    report = exporter.get_last_export_report()
    assert report is not None
    assert not report.success
    assert report.validation_failures
    assert report.generated_files == []


def test_export_neutralizes_traversal_attempt_in_project_name(tmp_path):
    """``project_name`` is user-provided text used to build a directory name.

    It goes through slugification before it ever reaches a path join, so
    ``../../escape`` becomes a harmless literal directory name rather than a
    traversal -- the output must land inside ``tmp_path``, never above it.
    """
    tree = FocusTree(id="safe_tree", tree_name="safe_tree")
    tree.focuses = [Focus(id="root", name="root")]
    exporter = HOI4Exporter()

    ok = exporter.export_tree(
        tree,
        str(tmp_path),
        project_name="../../escape",
        write_localisation=False,
        write_gfx=False,
    )

    assert ok is True
    report = exporter.get_last_export_report()
    focus_path = Path(report.focus_path).resolve()
    assert tmp_path.resolve() in focus_path.parents


def test_export_report_lists_generated_files(tmp_path):
    tree = FocusTree(id="report_tree", tree_name="report_tree")
    tree.focuses = [Focus(id="root", name="root")]
    exporter = HOI4Exporter()

    ok = exporter.export_tree(tree, str(tmp_path), write_localisation=False, write_gfx=False)

    assert ok is True
    report = exporter.get_last_export_report()
    assert report.success
    assert report.focus_path in report.generated_files
    assert Path(report.focus_path).exists()


LOCALIZATION_CASES = [
    ('Say "Hello"', "Quoted name"),
    ("Back\\slash path", "Escaped\\value"),
    ("Café \u00e9\u00e8 unicode", "Ünïcödé"),
    ("Multi\nline", "Second\nline"),
    ("Time: 12:00, ratio 3:1", "Colon: heavy: text"),
]


@pytest.mark.parametrize("name,desc", LOCALIZATION_CASES)
def test_localisation_escapes_quotes_backslashes_unicode_newlines_colons(tmp_path, name, desc):
    tree = FocusTree(id="loc_tree", tree_name="loc_tree")
    tree.focuses = [Focus(id="loc_focus", name=name, description=desc)]
    exporter = HOI4Exporter()

    ok = exporter.export_tree(tree, str(tmp_path), write_localisation=True, write_gfx=False)
    assert ok is True

    report = exporter.get_last_export_report()
    loc_path = Path(next(iter(report.localisation_paths.values())))
    content = loc_path.read_text(encoding="utf-8-sig")

    from _paradox_lexer import tokenize

    tokens, diagnostics = tokenize(content)
    assert diagnostics == [], f"localisation output is not valid script text: {diagnostics}"

    string_tokens = [t for t in tokens if t.text.startswith('"')]
    decoded_values = [t.value for t in string_tokens]
    expected_name = name.replace("\n", "\\n")
    expected_desc = desc.replace("\n", "\\n")
    assert expected_name in decoded_values
    assert expected_desc in decoded_values
    assert content.count('"') % 2 == 0  # every quote is paired -- no literal break-out
