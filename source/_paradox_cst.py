"""Concrete syntax tree and recursive-descent parser for Paradox script.

Built on :mod:`_paradox_lexer`. The tree is *concrete*: every node records the
exact character span it came from, so any subtree can be re-exported verbatim by
slicing the original source. Nothing is normalized, reordered, or rewritten
during parsing, and unknown constructs are kept as ordinary nodes rather than
discarded.

Grammar subset
--------------
::

    document    := node* END
    node        := assignment | value
    assignment  := key operator value
    key         := IDENTIFIER | NUMBER | STRING
    operator    := '=' | '==' | '>' | '<' | '>=' | '<='
    value       := IDENTIFIER | NUMBER | STRING | block
    block       := '{' node* '}'

A ``node`` list may freely mix assignments (``x = 0``) and anonymous values
(``FOCUS_FILTER_POLITICAL``, or a bare ``{ ... }``), which is how Paradox writes
list-like blocks. Repeated keys are legal and are kept in source order.

Unsupported constructs
----------------------
Paradox variants that HOI4 focus files do not use are intentionally *not* given
dedicated syntax: ``@variables``, ``[[parameter]]`` blocks, and inline math
``@[a+b]`` all lex as ordinary identifiers and are preserved verbatim rather
than interpreted. RGB/HSV colour prefixes are likewise plain identifiers
followed by a block.

Recovery strategy
-----------------
With ``recover=True`` the parser never stops at the first problem, but every
recovery emits a :class:`~_paradox_lexer.Diagnostic`:

``unexpected-closing-brace``  a ``}`` with no matching ``{``; the brace is skipped.
``unterminated-block``        end of file inside a block; the block is closed
                              implicitly at EOF and anchored to its ``{``.
``missing-value``             an operator with no value after it; the key is
                              demoted to an anonymous value.
``unexpected-operator``       an operator with no key before it; it is skipped.
``nesting-too-deep``          more than ``Parser.DEFAULT_MAX_DEPTH`` nested
                              blocks; the block keeps its span (so it still
                              re-exports) but is not descended into.

With ``recover=False`` the first error raises
:class:`~_paradox_lexer.ParadoxSyntaxError` instead -- there is no silent
fallback for structurally invalid input in either mode.

No Qt, no project imports beyond the lexer: standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Union

from _paradox_lexer import (
    ATOM_KINDS,
    Diagnostic,
    Lexer,
    ParadoxSyntaxError,
    SourceText,
    Token,
    TokenKind,
)

__all__ = [
    "Scalar",
    "Block",
    "Assignment",
    "Document",
    "Node",
    "Parser",
    "parse",
    "parse_file",
    "ParadoxSyntaxError",
    "Diagnostic",
    "SourceText",
]


@dataclass
class Scalar:
    """A leaf value or key: an identifier, a number, or a quoted string."""

    source: SourceText
    token: Token

    @property
    def start(self) -> int:
        return self.token.start

    @property
    def end(self) -> int:
        return self.token.end

    @property
    def line(self) -> int:
        return self.token.line

    @property
    def column(self) -> int:
        return self.token.column

    @property
    def text(self) -> str:
        """Verbatim source slice, quotes included."""
        return self.token.text

    @property
    def value(self) -> str:
        """Decoded text: quotes stripped and escapes resolved for strings."""
        return self.token.value

    @property
    def is_quoted(self) -> bool:
        return self.token.kind is TokenKind.STRING

    @property
    def is_number(self) -> bool:
        return self.token.kind is TokenKind.NUMBER

    def as_number(self) -> Optional[Union[int, float]]:
        """Return the numeric value, or ``None`` if this scalar is not numeric."""
        raw = self.value.strip()
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            return None

    def as_bool(self) -> Optional[bool]:
        """Interpret Paradox truthiness (``yes``/``no``); ``None`` if unclear."""
        lowered = self.value.strip().lower()
        if lowered in ("yes", "true", "1"):
            return True
        if lowered in ("no", "false", "0"):
            return False
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Scalar({self.value!r}@{self.line}:{self.column})"


class _Container:
    """Shared lookup helpers for anything holding a list of child nodes.

    Key lookups are case-insensitive, matching how the previous importer behaved
    and how the game itself treats keys in practice.
    """

    children: List["Node"]
    source: SourceText

    def assignments(self) -> List["Assignment"]:
        """Direct ``key = value`` children, in source order."""
        return [child for child in self.children if isinstance(child, Assignment)]

    def anonymous(self) -> List[Union[Scalar, "Block"]]:
        """Direct children that are bare values rather than assignments."""
        return [child for child in self.children if not isinstance(child, Assignment)]

    def get_all(self, key: str) -> List["Assignment"]:
        """Every direct assignment with ``key``, in source order (duplicates kept)."""
        wanted = key.lower()
        return [a for a in self.assignments() if a.key.value.lower() == wanted]

    def get(self, key: str) -> Optional["Assignment"]:
        """First direct assignment with ``key``, or ``None``."""
        matches = self.get_all(key)
        return matches[0] if matches else None

    def scalar(self, key: str) -> Optional[Scalar]:
        """First direct assignment with ``key`` whose value is a scalar."""
        for assignment in self.get_all(key):
            if isinstance(assignment.value, Scalar):
                return assignment.value
        return None

    def block(self, key: str) -> Optional["Block"]:
        """First direct assignment with ``key`` whose value is a block."""
        for assignment in self.get_all(key):
            if isinstance(assignment.value, Block):
                return assignment.value
        return None

    def blocks(self, key: str) -> List["Block"]:
        """Every direct assignment with ``key`` whose value is a block."""
        return [a.value for a in self.get_all(key) if isinstance(a.value, Block)]

    def keys(self) -> List[str]:
        """Direct assignment keys in source order, duplicates included."""
        return [a.key.value for a in self.assignments()]

    def walk(self) -> Iterator["Node"]:
        """Depth-first pre-order traversal of this container's descendants."""
        for child in self.children:
            yield child
            if isinstance(child, Assignment):
                yield child.value
                if isinstance(child.value, Block):
                    yield from child.value.walk()
            elif isinstance(child, Block):
                yield from child.walk()

    def find_all(self, key: str) -> List["Assignment"]:
        """Every assignment with ``key`` at any depth, in source order."""
        wanted = key.lower()
        found = [
            node
            for node in self.walk()
            if isinstance(node, Assignment) and node.key.value.lower() == wanted
        ]
        return found


@dataclass
class Block(_Container):
    """A ``{ ... }`` group. ``rbrace`` is ``None`` for an unterminated block."""

    source: SourceText
    lbrace: Token
    rbrace: Optional[Token]
    start: int
    end: int
    inner_start: int
    inner_end: int
    children: List["Node"] = field(default_factory=list)

    @property
    def is_terminated(self) -> bool:
        return self.rbrace is not None

    @property
    def line(self) -> int:
        return self.lbrace.line

    @property
    def column(self) -> int:
        return self.lbrace.column

    @property
    def text(self) -> str:
        """Verbatim source of the block, braces included."""
        return self.source.slice(self.start, self.end)

    @property
    def inner_text(self) -> str:
        """Verbatim source *between* the braces."""
        return self.source.slice(self.inner_start, self.inner_end)

    def value_strings(self) -> List[str]:
        """Decoded text of every direct anonymous scalar, in source order."""
        return [child.value for child in self.anonymous() if isinstance(child, Scalar)]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Block({len(self.children)} children @{self.line}:{self.column})"


@dataclass
class Assignment:
    """A ``key = value`` pair; ``value`` is a :class:`Scalar` or a :class:`Block`."""

    source: SourceText
    key: Scalar
    operator: Token
    value: Union[Scalar, "Block"]

    @property
    def start(self) -> int:
        return self.key.start

    @property
    def end(self) -> int:
        return self.value.end

    @property
    def line(self) -> int:
        return self.key.line

    @property
    def column(self) -> int:
        return self.key.column

    @property
    def op(self) -> str:
        return self.operator.text

    @property
    def text(self) -> str:
        """Verbatim source of the whole assignment."""
        return self.source.slice(self.start, self.end)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Assignment({self.key.value!r} {self.op} ...@{self.line}:{self.column})"


Node = Union[Assignment, Scalar, Block]


@dataclass
class Document(_Container):
    """A parsed file: top-level nodes plus the tokens and diagnostics behind them."""

    source: SourceText
    children: List[Node] = field(default_factory=list)
    tokens: List[Token] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return self.source.filename

    @property
    def text(self) -> str:
        return self.source.text

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error]

    @property
    def ok(self) -> bool:
        """True when nothing needed recovering."""
        return not self.errors

    def text_of(self, node: Node) -> str:
        """Verbatim source of any node from this document."""
        return self.source.slice(node.start, node.end)

    def raise_on_error(self) -> None:
        """Raise :class:`ParadoxSyntaxError` if any error diagnostic was recorded."""
        errors = self.errors
        if errors:
            raise ParadoxSyntaxError(errors)


class Parser:
    """Recursive-descent parser over the lexer's token stream."""

    #: Nesting depth beyond which a block is kept but not descended into.
    #: Real focus files nest a handful of levels deep; anything approaching this
    #: is malformed or hostile, and recursive descent would hit the interpreter
    #: recursion limit and die with a RecursionError instead of a diagnostic.
    DEFAULT_MAX_DEPTH = 200

    def __init__(
        self,
        source: SourceText,
        recover: bool = True,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self.source = source
        self.recover = recover
        self.max_depth = max_depth
        self.diagnostics: List[Diagnostic] = []
        self._tokens: List[Token] = []
        self._significant: List[Token] = []
        self._index = 0
        self._depth = 0

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def _report(
        self,
        code: str,
        message: str,
        token: Token,
        expected: str,
        severity: str = "error",
    ) -> Diagnostic:
        line, column = self.source.position(token.start)
        diagnostic = Diagnostic(
            severity=severity,
            code=code,
            message=message,
            filename=self.source.filename,
            line=line,
            column=column,
            offset=token.start,
            token=token.describe(),
            expected=expected,
            context=self.source.context(token.start, max(1, token.end - token.start)),
        )
        self.diagnostics.append(diagnostic)
        if severity == "error" and not self.recover:
            raise ParadoxSyntaxError([diagnostic])
        return diagnostic

    # ------------------------------------------------------------------
    # Token cursor
    # ------------------------------------------------------------------
    def _peek(self) -> Token:
        return self._significant[self._index]

    def _advance(self) -> Token:
        token = self._significant[self._index]
        if token.kind is not TokenKind.END:
            self._index += 1
        return token

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def parse(self) -> Document:
        lexer = Lexer(self.source, recover=self.recover)
        self._tokens = lexer.tokenize()
        self.diagnostics.extend(lexer.diagnostics)
        self._significant = [t for t in self._tokens if not t.is_trivia]
        self._index = 0

        children = self._parse_nodes(at_top_level=True)

        return Document(
            source=self.source,
            children=children,
            tokens=self._tokens,
            diagnostics=list(self.diagnostics),
        )

    # ------------------------------------------------------------------
    # Productions
    # ------------------------------------------------------------------
    def _parse_nodes(self, at_top_level: bool) -> List[Node]:
        """Parse ``node*`` until ``}`` (inside a block) or end of file."""
        children: List[Node] = []

        while True:
            token = self._peek()

            if token.kind is TokenKind.END:
                return children

            if token.kind is TokenKind.RBRACE:
                if not at_top_level:
                    return children
                self._advance()
                self._report(
                    code="unexpected-closing-brace",
                    message="closing brace without a matching opening brace",
                    token=token,
                    expected="a key, a value, or end of file",
                )
                continue

            if token.kind is TokenKind.OPERATOR:
                self._advance()
                self._report(
                    code="unexpected-operator",
                    message=f"operator {token.text!r} has no key before it",
                    token=token,
                    expected="a key before the operator",
                )
                continue

            if token.kind is TokenKind.LBRACE:
                children.append(self._parse_block())
                continue

            # An atom: either a key that starts an assignment, or a bare value.
            atom = self._advance()
            following = self._peek()
            if following.kind is TokenKind.OPERATOR:
                children.append(self._parse_assignment(atom))
            else:
                children.append(Scalar(self.source, atom))

    def _parse_assignment(self, key_token: Token) -> Node:
        """Parse ``key operator value``, having already consumed the key."""
        operator = self._advance()
        token = self._peek()

        if token.kind is TokenKind.LBRACE:
            value: Union[Scalar, Block] = self._parse_block()
        elif token.kind in ATOM_KINDS:
            value = Scalar(self.source, self._advance())
        else:
            # `key =` followed by `}` or EOF: keep the key as an anonymous value
            # so nothing is lost, and say exactly what was missing.
            self._report(
                code="missing-value",
                message=f"no value after {key_token.text!r} {operator.text!r}",
                token=token,
                expected="an identifier, number, string, or '{'",
            )
            return Scalar(self.source, key_token)

        return Assignment(
            source=self.source,
            key=Scalar(self.source, key_token),
            operator=operator,
            value=value,
        )

    def _parse_block(self) -> Block:
        """Parse ``'{' node* '}'``, having peeked the opening brace."""
        lbrace = self._advance()

        if self._depth >= self.max_depth:
            self._report(
                code="nesting-too-deep",
                message=(
                    f"block nesting exceeds the limit of {self.max_depth}; "
                    "its contents are kept verbatim but not parsed"
                ),
                token=lbrace,
                expected=f"at most {self.max_depth} levels of nested braces",
            )
            return self._skip_block(lbrace)

        self._depth += 1
        try:
            children = self._parse_nodes(at_top_level=False)
        finally:
            self._depth -= 1
        token = self._peek()

        if token.kind is TokenKind.RBRACE:
            rbrace = self._advance()
            return Block(
                source=self.source,
                lbrace=lbrace,
                rbrace=rbrace,
                start=lbrace.start,
                end=rbrace.end,
                inner_start=lbrace.end,
                inner_end=rbrace.start,
                children=children,
            )

        # End of file inside a block. Anchor the diagnostic to the '{' that was
        # never closed -- that is the position a user needs, not the EOF.
        self._report(
            code="unterminated-block",
            message="block opened here is never closed",
            token=lbrace,
            expected="a closing brace ('}')",
        )
        end = len(self.source)
        return Block(
            source=self.source,
            lbrace=lbrace,
            rbrace=None,
            start=lbrace.start,
            end=end,
            inner_start=lbrace.end,
            inner_end=end,
            children=children,
        )


    def _skip_block(self, lbrace: Token) -> Block:
        """Consume a block iteratively, keeping its span but not its structure.

        Used past the depth limit: the source is still addressable (and so still
        re-exportable) even though no child nodes are built for it.
        """
        depth = 1
        while True:
            token = self._peek()
            if token.kind is TokenKind.END:
                self._report(
                    code="unterminated-block",
                    message="block opened here is never closed",
                    token=lbrace,
                    expected="a closing brace ('}')",
                )
                end = len(self.source)
                return Block(
                    source=self.source,
                    lbrace=lbrace,
                    rbrace=None,
                    start=lbrace.start,
                    end=end,
                    inner_start=lbrace.end,
                    inner_end=end,
                    children=[],
                )

            self._advance()
            if token.kind is TokenKind.LBRACE:
                depth += 1
            elif token.kind is TokenKind.RBRACE:
                depth -= 1
                if depth == 0:
                    return Block(
                        source=self.source,
                        lbrace=lbrace,
                        rbrace=token,
                        start=lbrace.start,
                        end=token.end,
                        inner_start=lbrace.end,
                        inner_end=token.start,
                        children=[],
                    )


def parse(text: str, filename: str = "<string>", recover: bool = True) -> Document:
    """Parse Paradox script text into a :class:`Document`.

    With ``recover=False`` the first structural problem raises
    :class:`ParadoxSyntaxError`. With ``recover=True`` parsing continues and
    every recovery is recorded in :attr:`Document.diagnostics`.
    """
    return Parser(SourceText(text, filename), recover=recover).parse()


def parse_file(path: str, recover: bool = True, encoding: str = "utf-8-sig") -> Document:
    """Read ``path`` and parse it, using the path as the diagnostic filename."""
    with open(path, "r", encoding=encoding, errors="replace") as handle:
        return parse(handle.read(), filename=path, recover=recover)
