"""Structured writer for Paradox script -- the deterministic counterpart to
:mod:`_paradox_cst`.

Output is built as a tree of typed :class:`Node` objects, never by
interpolating strings directly. That is what lets callers keep the
distinctions the format actually has:

``Ident``    a bare atom, already known to be syntactically safe (a focus id,
             a keyword like ``yes``/``no`` used as an atom, ...)
``Num``      an ``int``/``float`` value
``Bool``     a Paradox boolean (rendered ``yes``/``no``)
``Str``      text that must round-trip as data, quoted and escaped
``Assign``   ``key operator value``
``Block``    an ordered ``{ ... }`` group, rendered multi-line or inline
``Raw``      a verbatim fragment (e.g. a CST slice for an unknown construct)
             copied through byte-for-byte, untouched
``BlankLine``an explicit blank line, so vertical spacing is a decision made
             once by policy rather than scattered ``lines.append("")`` calls

Rendering is a pure function of this tree: the same tree always renders to
the same text, and the choice of whether an atom needs quoting is made from
its actual content (does it contain a character the lexer would treat as a
delimiter?) rather than a spot-check for spaces.

No Qt, no project imports beyond the lexer's token-shape knowledge: standard
library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Union

__all__ = [
    "Node",
    "Ident",
    "Num",
    "Bool",
    "Str",
    "Raw",
    "Assign",
    "Block",
    "BlankLine",
    "escape_paradox_string",
    "needs_quoting",
    "atom",
    "value_node",
    "inline_block",
    "raw_lines_block",
    "block",
    "render",
    "from_cst_node",
    "from_cst_document",
]


# ---------------------------------------------------------------------------
# Node model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ident:
    """A bare atom the caller guarantees is already lexer-safe."""

    text: str


@dataclass(frozen=True)
class Num:
    value: Union[int, float]


@dataclass(frozen=True)
class Bool:
    value: bool


@dataclass(frozen=True)
class Str:
    """Text that must be quoted and escaped at render time."""

    value: str


@dataclass(frozen=True)
class Raw:
    """A verbatim fragment, copied through with no re-formatting.

    Used to preserve unknown or imported constructs exactly as parsed: the
    writer never has to understand a construct to keep it.
    """

    text: str


@dataclass(frozen=True)
class BlankLine:
    """An explicit blank line between sibling nodes."""


Scalar = Union[Ident, Num, Bool, Str, Raw]


@dataclass(frozen=True)
class Block:
    """An ordered ``{ ... }`` group.

    ``inline`` renders every child on one line inside the braces (matching
    how Paradox itself writes short lists like ``prerequisite = { focus = x }``);
    otherwise each child is one line, indented one level deeper than the
    block's own line.
    """

    children: List["Node"] = field(default_factory=list)
    inline: bool = False


@dataclass(frozen=True)
class Assign:
    """``key operator value``. ``key`` is always a bare atom in HOI4 script."""

    key: str
    value: Union[Scalar, Block]
    operator: str = "="


Node = Union[Scalar, Block, Assign, BlankLine]


# ---------------------------------------------------------------------------
# Escaping and quoting
# ---------------------------------------------------------------------------

#: Characters that stop a bare atom in the lexer (see _paradox_lexer._ATOM_STOP).
#: Kept as an independent literal rather than importing the lexer's private
#: constant, since the writer must be correct even if the lexer never changes.
_ATOM_STOP = frozenset(' \t\r\n\v\f{}=<>#"\\')

#: Single-character escapes applied inside a quoted string. Anything else
#: below U+0020 has no textual representation in Paradox script and is
#: dropped rather than silently corrupting the file with a raw control byte.
_STRING_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t"}


def escape_paradox_string(text: str) -> str:
    """Escape ``text`` for use inside a double-quoted Paradox string.

    Centralizes every rule for what a quoted string may contain: backslashes
    and quotes are escaped, newlines and tabs become their two-character
    escapes, carriage returns are dropped (Windows line endings collapse to
    ``\\n``), and any other C0 control character is dropped rather than
    emitted raw, since Paradox script has no other way to represent it.
    """

    out: List[str] = []
    for ch in text:
        if ch in _STRING_ESCAPES:
            out.append(_STRING_ESCAPES[ch])
        elif ch == "\r":
            continue
        elif ord(ch) < 0x20:
            continue
        else:
            out.append(ch)
    return "".join(out)


def needs_quoting(text: str) -> bool:
    """True when ``text`` cannot be written as a bare atom.

    This is a structural check -- any character the lexer would treat as a
    delimiter, plus the empty string -- never a check for spaces alone, so a
    value like ``value=1`` or a literal ``#`` is caught just as reliably as a
    value with a space in it.
    """

    if not text:
        return True
    return any(ch in _ATOM_STOP for ch in text)


def atom(text: str) -> Union[Ident, Str]:
    """Wrap ``text`` as an :class:`Ident` if safe, else a quoted :class:`Str`."""

    return Str(text) if needs_quoting(text) else Ident(text)


def value_node(value) -> Scalar:
    """Convert a Python value to the appropriate scalar node.

    ``bool`` is checked before ``int`` since ``bool`` is a subclass of
    ``int`` in Python.
    """

    if isinstance(value, bool):
        return Bool(value)
    if isinstance(value, (int, float)):
        return Num(value)
    if isinstance(value, str):
        return atom(value)
    raise TypeError(f"cannot convert {value!r} of type {type(value).__name__} to a script node")


def raw_lines_block(text: str, *, skip_blank: bool = False) -> Block:
    """Build a block whose children are the lines of ``text``, one per line.

    Used for the free-form script blobs the domain model stores as opaque
    strings (``available``, ``bypass``, ``completion_reward``, ...): each
    line is re-indented to the block's depth by the ordinary scalar render
    path, exactly like any other child, rather than being pasted in with
    whatever indentation the line happened to already have.
    """

    lines = text.split("\n")
    if skip_blank:
        return Block(children=[Ident(line) for line in lines if line.strip()])
    return Block(children=[Ident(line) for line in lines])


def inline_block(children: List[Node]) -> Block:
    return Block(children=list(children), inline=True)


def block(children: List[Node]) -> Block:
    return Block(children=list(children), inline=False)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _format_number(value: Union[int, float]) -> str:
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = repr(value)
    return text


def _scalar_text(node: Scalar) -> str:
    if isinstance(node, Ident):
        return node.text
    if isinstance(node, Num):
        return _format_number(node.value)
    if isinstance(node, Bool):
        return "yes" if node.value else "no"
    if isinstance(node, Str):
        return f'"{escape_paradox_string(node.value)}"'
    if isinstance(node, Raw):
        return node.text
    raise TypeError(f"not a scalar node: {node!r}")


def _is_scalar(node: Node) -> bool:
    return isinstance(node, (Ident, Num, Bool, Str, Raw))


def render(nodes: List[Node], indent: str = "\t") -> str:
    """Render a top-level node list to Paradox script text.

    Deterministic: the same tree always produces the same text, with one
    newline-terminated line per node (blocks recurse), tab indentation one
    level per nesting depth, and blank lines only where a :class:`BlankLine`
    node was placed.
    """

    lines: List[str] = []
    _render_nodes(nodes, 0, indent, lines)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _render_nodes(nodes: List[Node], depth: int, indent: str, lines: List[str]) -> None:
    for node in nodes:
        _render_node(node, depth, indent, lines)


def _render_node(node: Node, depth: int, indent: str, lines: List[str]) -> None:
    pad = indent * depth

    if isinstance(node, BlankLine):
        lines.append("")
        return

    if isinstance(node, Raw):
        text = node.text
        if not text:
            return
        for line in text.split("\n"):
            lines.append(line)
        return

    if isinstance(node, Assign):
        key_text = node.key
        value = node.value
        if isinstance(value, Block):
            if value.inline:
                inner = _render_inline_children(value.children, indent)
                lines.append(f"{pad}{key_text} {node.operator} {{ {inner} }}" if inner else f"{pad}{key_text} {node.operator} {{ }}")
            else:
                lines.append(f"{pad}{key_text} {node.operator} {{")
                _render_nodes(value.children, depth + 1, indent, lines)
                lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{key_text} {node.operator} {_scalar_text(value)}")
        return

    if isinstance(node, Block):
        if node.inline:
            inner = _render_inline_children(node.children, indent)
            lines.append(f"{pad}{{ {inner} }}" if inner else f"{pad}{{ }}")
        else:
            lines.append(f"{pad}{{")
            _render_nodes(node.children, depth + 1, indent, lines)
            lines.append(f"{pad}}}")
        return

    if _is_scalar(node):
        lines.append(f"{pad}{_scalar_text(node)}")
        return

    raise TypeError(f"unknown node type: {node!r}")


# ---------------------------------------------------------------------------
# Bridge from the parsed concrete syntax tree (_paradox_cst)
# ---------------------------------------------------------------------------

def from_cst_node(node) -> Node:
    """Convert one :mod:`_paradox_cst` node into a writer node, verbatim.

    Every scalar (identifier, number, or quoted string) becomes a
    :class:`Raw` carrying its exact source text -- quotes, escapes, and
    number spelling included -- so a construct the semantic layer does not
    understand is preserved exactly rather than reformatted or dropped.
    Blocks and assignments recurse, keeping source order.
    """

    # Imported lazily to avoid a hard import-time dependency between the
    # writer and the CST module for callers that only need one of the two.
    from _paradox_cst import Assignment, Block as CstBlock, Scalar

    if isinstance(node, Scalar):
        return Raw(node.text)
    if isinstance(node, CstBlock):
        return Block(children=[from_cst_node(child) for child in node.children], inline=False)
    if isinstance(node, Assignment):
        return Assign(node.key.text, from_cst_node(node.value), operator=node.op)
    raise TypeError(f"unsupported CST node type: {type(node).__name__}")


def from_cst_document(document) -> List[Node]:
    """Convert every top-level child of a parsed ``Document`` to writer nodes."""

    return [from_cst_node(child) for child in document.children]


def _render_inline_children(children: List[Node], indent: str) -> str:
    parts: List[str] = []
    for child in children:
        sub: List[str] = []
        _render_node(child, 0, indent, sub)
        parts.append(" ".join(line.strip() for line in sub if line.strip()))
    return " ".join(part for part in parts if part)
