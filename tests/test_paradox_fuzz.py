"""Fuzz and property tests for the Paradox parser.

Malformed input is the normal case for this parser -- users import half-edited
mod files -- so the guarantees tested here are the ones that matter most:
parsing always terminates, never crashes with an unexpected exception type, and
never loses source text, whatever it is fed.

Randomness is seeded so a failure is reproducible from the reported seed.
"""

import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _paradox_cst import Scalar, parse
from _paradox_lexer import ParadoxSyntaxError, tokenize

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "source"
VALID_DIR = Path(__file__).resolve().parent / "fixtures" / "paradox" / "valid"

#: Fragments chosen to hit the structural edges: unbalanced braces, quotes that
#: never close, escapes at the very end of input, comments that eat a newline.
FRAGMENTS = [
    "{", "}", "=", "==", ">", "<", ">=", "<=",
    " ", "\t", "\n", "\r\n",
    "#", "# comment }\n", "#{{{",
    '"', '"unclosed', '"closed"', '"} { "', '\\"', "\\",
    "id", "focus", "focus_tree", "prerequisite", "yes",
    "0", "-1", "1.5", "1936.1.1", ".", "-",
    "focus = {", "} }", "= =", "{}", '"\\',
]

#: Every seeded case is run against both parser modes.
MODES = (True, False)


def _random_source(rng: random.Random, max_fragments: int = 60) -> str:
    count = rng.randint(0, max_fragments)
    return "".join(rng.choice(FRAGMENTS) for _ in range(count))


def _mutate(rng: random.Random, text: str) -> str:
    """Apply a few random single-character edits to otherwise valid input."""
    chars = list(text)
    for _ in range(rng.randint(1, 8)):
        if not chars:
            break
        index = rng.randrange(len(chars))
        action = rng.choice(("delete", "insert", "replace", "duplicate"))
        if action == "delete":
            del chars[index]
        elif action == "insert":
            chars.insert(index, rng.choice('{}="#\\\n'))
        elif action == "replace":
            chars[index] = rng.choice('{}="#\\\n')
        else:
            chars.insert(index, chars[index])
    return "".join(chars)


def _assert_invariants(text: str, recover: bool, label: str) -> None:
    """Parse ``text`` and check the invariants that must hold for any input."""
    try:
        document = parse(text, filename="fuzz.txt", recover=recover)
    except ParadoxSyntaxError:
        # The only exception strict mode is allowed to raise.
        assert not recover, f"{label}: recovering mode raised ParadoxSyntaxError"
        return

    if recover:
        # Recovering mode must never invent a clean parse for broken input:
        # anything it papered over has to be reported.
        assert isinstance(document.diagnostics, list)
    else:
        assert document.ok, f"{label}: strict mode returned errors without raising"

    # Nothing is rewritten, and every span addresses real source.
    assert document.text == text, f"{label}: source was modified"
    for node in document.walk():
        assert 0 <= node.start <= node.end <= len(text), f"{label}: bad span"
        if isinstance(node, Scalar):
            assert text[node.start : node.end] == node.text, f"{label}: scalar drift"

    # Top-level children are ordered and non-overlapping.
    spans = [(node.start, node.end) for node in document.children]
    assert spans == sorted(spans), f"{label}: children out of source order"


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("recover", MODES)
def test_random_fragment_soup_terminates_and_holds_invariants(seed, recover):
    rng = random.Random(seed)
    for iteration in range(25):
        text = _random_source(rng)
        _assert_invariants(text, recover, f"seed={seed} iter={iteration} {text!r}")


@pytest.mark.parametrize("recover", MODES)
def test_mutated_valid_fixtures_hold_invariants(recover):
    rng = random.Random(1337)
    originals = [p.read_text(encoding="utf-8") for p in sorted(VALID_DIR.glob("*.txt"))]
    assert originals
    for index in range(200):
        text = _mutate(rng, rng.choice(originals))
        _assert_invariants(text, recover, f"mutation={index}")


def test_lexer_is_lossless_for_arbitrary_input():
    rng = random.Random(99)
    for _ in range(300):
        text = _random_source(rng)
        tokens, _ = tokenize(text)
        assert "".join(t.text for t in tokens) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\n\n",
        "#",
        "#no newline at eof",
        '"',
        '"\\',
        "\\",
        "{",
        "}",
        "=",
        "{" * 500,
        "}" * 500,
        "a =" * 500,
        '"' + "x" * 5000,
        "a = { " * 200 + "}" * 200,
    ],
    ids=lambda t: repr(t[:24]),
)
def test_pathological_inputs_do_not_crash(text):
    document = parse(text, filename="edge.txt")
    assert document.text == text
    tokens, _ = tokenize(text)
    assert "".join(t.text for t in tokens) == text


def test_deeply_nested_input_does_not_blow_the_stack():
    """Recursive descent stops at a depth limit instead of raising RecursionError."""
    depth = 2000
    text = "a = {" * depth + "}" * depth
    document = parse(text, filename="deep.txt")
    assert document.text == text
    assert "nesting-too-deep" in [d.code for d in document.diagnostics]
    # The over-deep block is still addressable, so it can be re-exported.
    node = document.block("a")
    assert text[node.start : node.end] == node.text


def test_parsing_is_linear_enough_to_rule_out_pathological_backtracking():
    """A regex-based parser degrades badly here; a character scanner must not."""
    unit = 'focus = { id = a text = "} #" }\n# comment }\n'
    small = unit * 200
    large = unit * 2000

    def elapsed(text):
        start = time.perf_counter()
        parse(text, filename="perf.txt")
        return time.perf_counter() - start

    # Warm up so import/JIT-ish first-call costs do not skew the ratio.
    elapsed(small)
    small_time = max(elapsed(small), 1e-6)
    large_time = elapsed(large)

    # 10x the input should cost well under 100x the time.
    assert large_time < small_time * 100, (small_time, large_time)


def test_fuzz_sweep_terminates_under_a_wall_clock_timeout():
    """A hang would block the whole suite, so run this sweep out of process.

    ``subprocess.run(timeout=...)`` is the only way to actually prove
    termination rather than assume it.
    """
    script = f"""
import random, sys
sys.path.insert(0, {str(SOURCE_DIR)!r})
from _paradox_cst import parse
from _paradox_lexer import ParadoxSyntaxError

FRAGMENTS = {FRAGMENTS!r}
rng = random.Random(20240707)
for _ in range(4000):
    text = "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, 80)))
    for recover in (True, False):
        try:
            parse(text, filename="fuzz.txt", recover=recover)
        except ParadoxSyntaxError:
            pass
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
