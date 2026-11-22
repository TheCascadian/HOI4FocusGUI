"""Lexer for Paradox script -- the plain-text format HOI4 uses for focus trees.

This module owns the *lexical* layer only: it turns raw source text into a flat,
lossless list of :class:`Token` objects. Every character of the input is covered
by exactly one token (whitespace and comments included), so the token stream can
be re-joined into the original text byte for byte.

It is deliberately hand-written rather than regex-driven, because the constructs
that break regex/brace-counting importers -- braces inside comments, braces
inside quoted strings, escaped quotes -- are exactly the ones a character
scanner handles naturally.

No Qt, no project imports: this module depends on the standard library only.

Recognized tokens
-----------------
``IDENTIFIER``   bare atom that is not numeric (``sample_root``, ``yes``,
                 ``1936.1.1``, ``GFX_goal_generic_navy_doctrine``)
``NUMBER``       bare atom matching ``-?digits[.digits]`` (``10``, ``-2``, ``0.05``)
``STRING``       double-quoted run, with ``\\"`` and ``\\\\`` escapes decoded into
                 :attr:`Token.value` while :attr:`Token.text` keeps the source
``OPERATOR``     ``=``, ``==``, ``>``, ``<``, ``>=``, ``<=``
``LBRACE``       ``{``
``RBRACE``       ``}``
``COMMENT``      ``#`` to end of line (the newline itself is whitespace)
``WHITESPACE``   any run of spaces, tabs, newlines
``END``          zero-width sentinel at end of input
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "TokenKind",
    "Token",
    "SourceText",
    "Diagnostic",
    "ParadoxSyntaxError",
    "Lexer",
    "tokenize",
]


class TokenKind(str, Enum):
    """Lexical category of a token."""

    IDENTIFIER = "identifier"
    NUMBER = "number"
    STRING = "string"
    OPERATOR = "operator"
    LBRACE = "lbrace"
    RBRACE = "rbrace"
    COMMENT = "comment"
    WHITESPACE = "whitespace"
    END = "end"


#: Token kinds that can stand as a key or as a scalar value.
ATOM_KINDS = frozenset({TokenKind.IDENTIFIER, TokenKind.NUMBER, TokenKind.STRING})

#: Token kinds the parser skips over; they stay in the stream for re-export.
TRIVIA_KINDS = frozenset({TokenKind.WHITESPACE, TokenKind.COMMENT})

_WHITESPACE = " \t\r\n\v\f"
#: Characters that terminate a bare atom.
_ATOM_STOP = frozenset(_WHITESPACE) | frozenset('{}=<>#"')


@dataclass(frozen=True)
class Token:
    """A single lexeme plus its exact span in the source.

    ``text`` is always the verbatim source slice (``source[start:end]``).
    ``value`` is the decoded payload: for ``STRING`` the contents with quotes
    stripped and escapes resolved, and the same as ``text`` for everything else.
    """

    kind: TokenKind
    text: str
    start: int
    end: int
    line: int
    column: int
    value: str = ""

    def __post_init__(self) -> None:
        if not self.value and self.kind is not TokenKind.STRING:
            object.__setattr__(self, "value", self.text)

    @property
    def is_trivia(self) -> bool:
        return self.kind in TRIVIA_KINDS

    @property
    def is_atom(self) -> bool:
        return self.kind in ATOM_KINDS

    def describe(self) -> str:
        """Short human-readable form used in diagnostics."""
        if self.kind is TokenKind.END:
            return "end of file"
        return repr(self.text)


class SourceText:
    """Immutable source buffer that can map offsets to line/column and context.

    Holding the original text alongside the token stream is what makes
    ``unknown syntax is retained`` cheap: any CST node can be re-exported by
    slicing ``source[node.start:node.end]``, with no rewriting anywhere.
    """

    __slots__ = ("text", "filename", "_line_starts")

    def __init__(self, text: str, filename: str = "<string>") -> None:
        self.text = text
        self.filename = filename
        starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                starts.append(index + 1)
        self._line_starts = starts

    def __len__(self) -> int:
        return len(self.text)

    def slice(self, start: int, end: int) -> str:
        return self.text[start:end]

    def position(self, offset: int) -> Tuple[int, int]:
        """Return the 1-based ``(line, column)`` for a character offset."""
        offset = max(0, min(offset, len(self.text)))
        line_index = bisect.bisect_right(self._line_starts, offset) - 1
        return line_index + 1, offset - self._line_starts[line_index] + 1

    def line_text(self, line: int) -> str:
        """Return the text of a 1-based line, without its trailing newline."""
        if line < 1 or line > len(self._line_starts):
            return ""
        start = self._line_starts[line - 1]
        end = self._line_starts[line] if line < len(self._line_starts) else len(self.text)
        return self.text[start:end].rstrip("\n").rstrip("\r")

    def context(self, offset: int, length: int = 1) -> str:
        """Render the offending source line with a caret run underneath.

        Leading tabs are copied verbatim into the caret line so the marker stays
        aligned in terminals that render tabs at any width.
        """
        line, column = self.position(offset)
        text = self.line_text(line)
        prefix = "".join("\t" if ch == "\t" else " " for ch in text[: column - 1])
        marker = "^" * max(1, min(length, max(1, len(text) - column + 1)))
        return f"{text}\n{prefix}{marker}"


@dataclass(frozen=True)
class Diagnostic:
    """A structured parse problem.

    Carries everything a caller needs to report the failure precisely: which
    file, which line and column, the token that was actually found, the
    construct that was expected, and the surrounding source line.
    """

    severity: str
    code: str
    message: str
    filename: str
    line: int
    column: int
    offset: int
    token: str
    expected: str
    context: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable form, for embedding in converter output."""
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "filename": self.filename,
            "line": self.line,
            "column": self.column,
            "offset": self.offset,
            "token": self.token,
            "expected": self.expected,
            "context": self.context,
        }

    def format(self, include_context: bool = True) -> str:
        head = (
            f"{self.filename}:{self.line}:{self.column}: {self.severity} "
            f"[{self.code}]: {self.message}"
        )
        if include_context and self.context:
            indented = "\n".join("    " + part for part in self.context.split("\n"))
            return f"{head}\n{indented}"
        return head

    def __str__(self) -> str:
        return self.format(include_context=False)


class ParadoxSyntaxError(Exception):
    """Raised in strict mode when the input is not structurally valid."""

    def __init__(self, diagnostics: List[Diagnostic]) -> None:
        self.diagnostics = list(diagnostics)
        primary = self.diagnostics[0] if self.diagnostics else None
        super().__init__(primary.format() if primary else "invalid Paradox script")

    @property
    def diagnostic(self) -> Optional[Diagnostic]:
        return self.diagnostics[0] if self.diagnostics else None


def _is_number(text: str) -> bool:
    """Return True for ``-?digits`` / ``-?digits.digits`` and nothing else.

    Notably ``1936.1.1`` (a Paradox date) has two dots and stays an identifier,
    as do ``yes``/``no`` and bare tags.
    """
    body = text[1:] if text[:1] in ("-", "+") else text
    if not body:
        return False
    head, dot, tail = body.partition(".")
    if dot:
        if not tail or not tail.isdigit():
            return False
        return head.isdigit() or (head == "" and tail.isdigit())
    return head.isdigit()


class Lexer:
    """Character scanner producing a lossless token stream.

    In recovering mode (the default) lexical problems -- currently only
    unterminated strings -- are reported as diagnostics and scanning continues.
    In strict mode the first problem raises :class:`ParadoxSyntaxError`.
    """

    def __init__(self, source: SourceText, recover: bool = True) -> None:
        self.source = source
        self.recover = recover
        self.diagnostics: List[Diagnostic] = []

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def _report(
        self,
        code: str,
        message: str,
        offset: int,
        token: str,
        expected: str,
        length: int = 1,
        severity: str = "error",
    ) -> Diagnostic:
        line, column = self.source.position(offset)
        diagnostic = Diagnostic(
            severity=severity,
            code=code,
            message=message,
            filename=self.source.filename,
            line=line,
            column=column,
            offset=offset,
            token=token,
            expected=expected,
            context=self.source.context(offset, length),
        )
        self.diagnostics.append(diagnostic)
        if severity == "error" and not self.recover:
            raise ParadoxSyntaxError([diagnostic])
        return diagnostic

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def tokenize(self) -> List[Token]:
        text = self.source.text
        length = len(text)
        tokens: List[Token] = []
        index = 0

        while index < length:
            char = text[index]
            if char in _WHITESPACE:
                end = index + 1
                while end < length and text[end] in _WHITESPACE:
                    end += 1
                tokens.append(self._token(TokenKind.WHITESPACE, index, end))
                index = end
            elif char == "#":
                end = index
                while end < length and text[end] != "\n":
                    end += 1
                tokens.append(self._token(TokenKind.COMMENT, index, end))
                index = end
            elif char == "{":
                tokens.append(self._token(TokenKind.LBRACE, index, index + 1))
                index += 1
            elif char == "}":
                tokens.append(self._token(TokenKind.RBRACE, index, index + 1))
                index += 1
            elif char in "=<>":
                end = index + 1
                if end < length and text[end] == "=":
                    end += 1
                tokens.append(self._token(TokenKind.OPERATOR, index, end))
                index = end
            elif char == '"':
                token, index = self._scan_string(index)
                tokens.append(token)
            else:
                end = index + 1
                while end < length and text[end] not in _ATOM_STOP:
                    end += 1
                raw = text[index:end]
                kind = TokenKind.NUMBER if _is_number(raw) else TokenKind.IDENTIFIER
                tokens.append(self._token(kind, index, end))
                index = end

        tokens.append(self._token(TokenKind.END, length, length))
        return tokens

    def _token(self, kind: TokenKind, start: int, end: int, value: str = "") -> Token:
        line, column = self.source.position(start)
        return Token(
            kind=kind,
            text=self.source.slice(start, end),
            start=start,
            end=end,
            line=line,
            column=column,
            value=value,
        )

    def _scan_string(self, start: int) -> Tuple[Token, int]:
        """Scan a double-quoted string, decoding ``\\"`` and ``\\\\``.

        An unterminated string is reported and closed at the end of the line, so
        one stray quote cannot swallow the remainder of the file.
        """
        text = self.source.text
        length = len(text)
        index = start + 1
        decoded: List[str] = []
        terminated = False

        while index < length:
            char = text[index]
            if char == "\\" and index + 1 < length and text[index + 1] in '"\\':
                decoded.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                index += 1
                terminated = True
                break
            if char == "\n":
                break
            decoded.append(char)
            index += 1

        if not terminated:
            self._report(
                code="unterminated-string",
                message="string literal is not closed before the end of the line",
                offset=start,
                token=text[start:index],
                expected='closing double quote (")',
                length=max(1, index - start),
            )

        return (
            self._token(TokenKind.STRING, start, index, value="".join(decoded)),
            index,
        )


def tokenize(
    text: str, filename: str = "<string>", recover: bool = True
) -> Tuple[List[Token], List[Diagnostic]]:
    """Convenience wrapper: lex ``text`` and return ``(tokens, diagnostics)``."""
    lexer = Lexer(SourceText(text, filename), recover=recover)
    return lexer.tokenize(), lexer.diagnostics
