"""Tests for the Paradox lexer and concrete syntax tree
(source/_paradox_lexer.py, source/_paradox_cst.py).

These modules are pure Python with no Qt dependency, so they are exercised
directly with no GUI construction anywhere.

Covered here: token categories and spans, structural immunity to braces inside
comments and strings, duplicate keys, anonymous values, preservation of unknown
constructs for re-export, stable line/column diagnostics, strict vs. recovering
modes, and determinism.
"""

import ast
from pathlib import Path

import pytest

from _paradox_cst import Block, Scalar, parse, parse_file
from _paradox_lexer import ParadoxSyntaxError, SourceText, TokenKind, tokenize

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "paradox"
VALID_DIR = FIXTURES / "valid"
INVALID_DIR = FIXTURES / "invalid"

VALID_FIXTURES = sorted(VALID_DIR.glob("*.txt"))
INVALID_FIXTURES = sorted(INVALID_DIR.glob("*.txt"))

# Every invalid fixture, with the single diagnostic it must produce. These are
# pinned by line and column: the point of the rewrite is that a user gets the
# same, exact location every time, not a generic failure.
EXPECTED_DIAGNOSTICS = {
    "missing_value.txt": ("missing-value", 7, 2),
    "stray_closing_brace.txt": ("unexpected-closing-brace", 5, 1),
    "stray_operator.txt": ("unexpected-operator", 3, 2),
    "unclosed_block.txt": ("unterminated-block", 1, 14),
    "unterminated_string.txt": ("unterminated-string", 5, 10),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

def test_token_kinds_cover_the_documented_alphabet():
    tokens, diagnostics = tokenize('a = { b "c" 1 -2 0.5 } # trailing\n')
    assert diagnostics == []
    kinds = [t.kind for t in tokens if not t.is_trivia]
    assert kinds == [
        TokenKind.IDENTIFIER,
        TokenKind.OPERATOR,
        TokenKind.LBRACE,
        TokenKind.IDENTIFIER,
        TokenKind.STRING,
        TokenKind.NUMBER,
        TokenKind.NUMBER,
        TokenKind.NUMBER,
        TokenKind.RBRACE,
        TokenKind.END,
    ]
    assert any(t.kind is TokenKind.COMMENT for t in tokens)
    assert any(t.kind is TokenKind.WHITESPACE for t in tokens)


def test_dates_and_booleans_are_identifiers_not_numbers():
    tokens, _ = tokenize("date = 1936.1.1 flag = yes ratio = -0.25")
    by_text = {t.text: t.kind for t in tokens if t.is_atom}
    assert by_text["1936.1.1"] is TokenKind.IDENTIFIER
    assert by_text["yes"] is TokenKind.IDENTIFIER
    assert by_text["-0.25"] is TokenKind.NUMBER


def test_token_stream_is_lossless():
    text = _read(VALID_DIR / "vanilla_style.txt")
    tokens, _ = tokenize(text)
    assert "".join(t.text for t in tokens) == text


def test_string_escapes_are_decoded_but_source_is_kept():
    tokens, diagnostics = tokenize(r'name = "a \"quoted\" back\\slash"')
    assert diagnostics == []
    string = [t for t in tokens if t.kind is TokenKind.STRING][0]
    assert string.value == 'a "quoted" back\\slash'
    assert string.text == r'"a \"quoted\" back\\slash"'


def test_operators_include_comparisons():
    tokens, _ = tokenize("a > 1 b >= 2 c == 3 d < 4 e <= 5 f = 6")
    ops = [t.text for t in tokens if t.kind is TokenKind.OPERATOR]
    assert ops == [">", ">=", "==", "<", "<=", "="]


def test_source_text_reports_one_based_line_and_column():
    source = SourceText("a = 1\nbb = 2\n")
    assert source.position(0) == (1, 1)
    assert source.position(6) == (2, 1)
    assert source.position(7) == (2, 2)


# ---------------------------------------------------------------------------
# Structure: braces inside comments and strings
# ---------------------------------------------------------------------------

def test_braces_in_comments_do_not_affect_structure():
    document = parse("a = { # } } } not real\n b = 1 }")
    assert document.ok
    block = document.block("a")
    assert block is not None and block.is_terminated
    assert block.scalar("b").as_number() == 1


def test_braces_in_strings_do_not_affect_structure():
    document = parse('a = { b = "} { }" c = 2 }')
    assert document.ok
    block = document.block("a")
    assert block.scalar("b").value == "} { }"
    assert block.scalar("c").as_number() == 2


def test_escaped_quote_does_not_end_the_string_early():
    document = parse(r'a = { b = "x \" }" c = 3 }')
    assert document.ok
    assert document.block("a").scalar("c").as_number() == 3


# ---------------------------------------------------------------------------
# Structure: duplicates, anonymous values, nesting
# ---------------------------------------------------------------------------

def test_duplicate_keys_are_retained_in_source_order():
    document = parse("x = 1 x = 2 x = 3")
    assert [a.value.value for a in document.get_all("x")] == ["1", "2", "3"]
    assert document.keys() == ["x", "x", "x"]


def test_anonymous_values_inside_blocks_are_preserved():
    document = parse("filters = { A B C }")
    block = document.block("filters")
    assert block.value_strings() == ["A", "B", "C"]
    assert block.assignments() == []


def test_anonymous_block_inside_a_block_is_preserved():
    document = parse("outer = { { 1 2 } named = 3 }")
    outer = document.block("outer")
    anonymous = outer.anonymous()
    assert len(anonymous) == 1 and isinstance(anonymous[0], Block)
    assert anonymous[0].value_strings() == ["1", "2"]


def test_nested_blocks_nest():
    document = parse("a = { b = { c = { d = 1 } } }")
    depth_one = document.block("a").block("b").block("c")
    assert depth_one.scalar("d").as_number() == 1
    assert len(document.find_all("d")) == 1


def test_find_all_is_recursive_and_get_all_is_not():
    document = parse("a = { focus = x inner = { focus = y } }")
    block = document.block("a")
    assert [a.value.value for a in block.get_all("focus")] == ["x"]
    assert [a.value.value for a in block.find_all("focus")] == ["x", "y"]


def test_key_lookup_is_case_insensitive():
    document = parse("Focus = { ID = a }")
    assert document.block("focus").scalar("id").value == "a"


# ---------------------------------------------------------------------------
# Spans and re-export
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_every_node_span_slices_back_to_its_own_source(path):
    text = _read(path)
    document = parse(text, filename=path.name)
    for node in document.walk():
        assert 0 <= node.start <= node.end <= len(text)
        slice_ = text[node.start : node.end]
        if isinstance(node, Scalar):
            assert slice_ == node.text
        elif isinstance(node, Block):
            assert slice_.startswith("{")
            if node.is_terminated:
                assert slice_.endswith("}")
        else:
            assert slice_ == node.text


def test_unknown_constructs_are_retained_verbatim_for_re_export():
    text = _read(VALID_DIR / "unknown_mod_constructs.txt")
    document = parse(text, filename="unknown_mod_constructs.txt")
    tree = document.block("focus_tree")

    unknown = tree.block("kaiserreich_metadata")
    assert unknown is not None
    # The block round-trips byte for byte, braces and all.
    assert unknown.text == text[unknown.start : unknown.end]
    assert "someone { with } braces in their name" in [
        a.value.value for a in unknown.find_all("author")
    ]
    assert unknown.block("weights").value_strings() == ["1", "2", "3"]


def test_document_source_is_never_rewritten():
    text = _read(VALID_DIR / "unusual_spacing.txt")
    document = parse(text)
    assert document.text == text


# ---------------------------------------------------------------------------
# Valid corpus
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixtures_parse_without_diagnostics(path):
    document = parse(_read(path), filename=path.name)
    assert document.diagnostics == []
    assert document.ok


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixtures_also_parse_in_strict_mode(path):
    document = parse(_read(path), filename=path.name, recover=False)
    assert document.ok


def test_valid_fixture_corpus_is_not_empty():
    # Guards against a rename silently turning the parametrized suites into
    # zero-test no-ops.
    assert len(VALID_FIXTURES) >= 6
    assert len(INVALID_FIXTURES) >= 5


# ---------------------------------------------------------------------------
# Invalid corpus: stable diagnostics, no silent fallback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_invalid_fixtures_produce_the_expected_diagnostic(path):
    document = parse(_read(path), filename=path.name)
    expected_code, expected_line, expected_column = EXPECTED_DIAGNOSTICS[path.name]

    codes = [d.code for d in document.diagnostics]
    assert expected_code in codes, codes

    diagnostic = next(d for d in document.diagnostics if d.code == expected_code)
    assert (diagnostic.line, diagnostic.column) == (expected_line, expected_column)
    assert diagnostic.filename == path.name
    assert diagnostic.severity == "error"
    assert diagnostic.token
    assert diagnostic.expected
    assert diagnostic.context


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_diagnostic_context_points_at_the_reported_line(path):
    document = parse(_read(path), filename=path.name)
    diagnostic = document.diagnostics[0]
    source_line = _read(path).splitlines()[diagnostic.line - 1]
    assert diagnostic.context.split("\n")[0] == source_line.rstrip("\r")
    assert "^" in diagnostic.context


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_strict_mode_raises_instead_of_falling_back(path):
    with pytest.raises(ParadoxSyntaxError) as excinfo:
        parse(_read(path), filename=path.name, recover=False)

    error = excinfo.value
    assert error.diagnostic is not None
    expected_code, expected_line, expected_column = EXPECTED_DIAGNOSTICS[path.name]
    assert error.diagnostic.code == expected_code
    assert (error.diagnostic.line, error.diagnostic.column) == (
        expected_line,
        expected_column,
    )
    # The message a caller sees names the file, the position and the problem.
    assert path.name in str(error)


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_recovery_still_yields_a_usable_tree(path):
    """Every recovery is reported, but parsing continues past the problem."""
    document = parse(_read(path), filename=path.name)
    assert document.diagnostics
    assert document.children  # not an empty, silently-discarded result


def test_raise_on_error_reraises_recovered_diagnostics():
    document = parse("a = { b = 1", filename="x.txt")
    assert not document.ok
    with pytest.raises(ParadoxSyntaxError):
        document.raise_on_error()


def test_unterminated_block_is_anchored_to_its_opening_brace():
    document = parse("focus = {\n  id = a\n", filename="x.txt")
    diagnostic = document.diagnostics[0]
    assert diagnostic.code == "unterminated-block"
    assert (diagnostic.line, diagnostic.column) == (1, 9)
    # Content before the failure is still available.
    assert document.block("focus").scalar("id").value == "a"


def test_missing_value_keeps_the_key_as_an_anonymous_value():
    document = parse("a = { x = 1 y = }", filename="x.txt")
    assert [d.code for d in document.diagnostics] == ["missing-value"]
    block = document.block("a")
    assert block.scalar("x").as_number() == 1
    assert block.value_strings() == ["y"]


def test_stray_closing_brace_does_not_swallow_later_content():
    document = parse("} a = 1", filename="x.txt")
    assert [d.code for d in document.diagnostics] == ["unexpected-closing-brace"]
    assert document.scalar("a").as_number() == 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def _shape(node):
    """Structural fingerprint of a node: kind, span, and children."""
    if isinstance(node, Scalar):
        return ("scalar", node.start, node.end, node.value)
    if isinstance(node, Block):
        return ("block", node.start, node.end, tuple(_shape(c) for c in node.children))
    return (
        "assignment",
        node.start,
        node.end,
        node.key.value,
        node.op,
        _shape(node.value),
    )


@pytest.mark.parametrize(
    "path", VALID_FIXTURES + INVALID_FIXTURES, ids=lambda p: p.name
)
def test_parsing_is_deterministic(path):
    text = _read(path)
    first = parse(text, filename=path.name)
    second = parse(text, filename=path.name)
    assert [_shape(n) for n in first.children] == [_shape(n) for n in second.children]
    assert [d.to_dict() for d in first.diagnostics] == [
        d.to_dict() for d in second.diagnostics
    ]


# ---------------------------------------------------------------------------
# Layering
# ---------------------------------------------------------------------------

SOURCE_DIR = Path(__file__).resolve().parent.parent / "source"


@pytest.mark.parametrize(
    "module", ["_paradox_lexer", "_paradox_cst", "_txt_converter"]
)
def test_parsing_modules_are_independent_of_qt(module):
    """The parser must stay usable (and testable) without a Qt install."""
    tree = ast.parse((SOURCE_DIR / f"{module}.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for name in imported:
        assert not name.startswith("PyQt"), f"{module} imports {name}"
        assert name != "_imports", f"{module} imports the Qt-laden facade"


def test_parse_file_uses_the_path_as_the_diagnostic_filename(tmp_path):
    path = tmp_path / "broken.txt"
    path.write_text("a = {\n", encoding="utf-8")
    document = parse_file(str(path))
    assert document.diagnostics[0].filename == str(path)
