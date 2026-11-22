"""Validation, path safety, and reporting for HOI4 mod export.

Kept separate from the writer (:mod:`_paradox_writer`) and the exporter
(:mod:`_exporter`) so the rules for "is this identifier/filename/path safe to
write" have one place to live and one place to test. No Qt, no project
imports beyond the standard library.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

__all__ = [
    "ExportValidationError",
    "PathTraversalError",
    "ExportReport",
    "validate_focus_id",
    "validate_filename",
    "validate_localization_key",
    "validate_icon_name",
    "resolve_within",
]


class ExportValidationError(ValueError):
    """A value cannot be exported as-is.

    Carries the field and offending value so a caller can build an
    actionable diagnostic instead of a bare message.
    """

    def __init__(self, field_name: str, value: object, reason: str):
        self.field_name = field_name
        self.value = value
        self.reason = reason
        super().__init__(f"{field_name}={value!r}: {reason}")


class PathTraversalError(ExportValidationError):
    """A resolved path escaped the mod/project directory it must stay inside."""


# Paradox identifiers (focus ids, localization keys, icon/sprite names) are,
# in practice, ASCII letters, digits, and underscores. This is stricter than
# what the lexer accepts as a bare atom -- the lexer will happily tokenize
# almost anything as an identifier -- but a focus id with, say, a brace or a
# quote in it is a data-entry mistake, not a construct HOI4 supports.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Windows and POSIX both forbid these in filenames; POSIX only truly forbids
# '/' and NUL, but the rest make for unusable/ambiguous mod filenames.
_FILENAME_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def validate_focus_id(value: str, *, field_name: str = "focus id") -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ExportValidationError(
            field_name, value, "must be non-empty ASCII letters, digits, or underscores"
        )
    return value


def validate_localization_key(value: str, *, field_name: str = "localization key") -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ExportValidationError(
            field_name, value, "must be non-empty ASCII letters, digits, or underscores"
        )
    return value


def validate_icon_name(value: str, *, field_name: str = "icon name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExportValidationError(field_name, value, "must not be empty")
    if _FILENAME_FORBIDDEN_RE.search(value):
        raise ExportValidationError(
            field_name, value, "must not contain path separators or filesystem-reserved characters"
        )
    return value


def validate_filename(value: str, *, field_name: str = "filename") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExportValidationError(field_name, value, "must not be empty")
    if value in (".", ".."):
        raise ExportValidationError(field_name, value, "must not be '.' or '..'")
    if _FILENAME_FORBIDDEN_RE.search(value):
        raise ExportValidationError(
            field_name, value, "must not contain path separators or filesystem-reserved characters"
        )
    return value


def resolve_within(root: str, *parts: str, field_name: str = "path") -> str:
    """Join ``parts`` onto ``root`` and guarantee the result stays inside it.

    Every path the exporter writes to is built through this function. It
    rejects absolute path segments, ``..`` traversal, and symlink escapes in
    one place rather than trusting each call site to have sanitized its
    inputs.
    """

    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, *parts))
    root_real = os.path.realpath(root_abs)
    candidate_real = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([root_real, candidate_real])
    except ValueError:
        # Different drives on Windows: definitely not inside root.
        common = None
    if common != root_real:
        raise PathTraversalError(
            field_name, os.path.join(*parts) if parts else root, "resolves outside the target mod directory"
        )
    return candidate


@dataclass
class ExportReport:
    """Everything a caller needs to know about one export run.

    ``success`` is false if any validation failure blocked writing; files
    already written before a failure are still listed in ``generated_files``
    so a caller can see exactly what landed on disk.
    """

    generated_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    validation_failures: List[str] = field(default_factory=list)
    copied_assets: List[str] = field(default_factory=list)
    skipped_assets: List[str] = field(default_factory=list)
    focus_path: Optional[str] = None
    localisation_paths: dict = field(default_factory=dict)
    gfx_path: Optional[str] = None
    project_root: Optional[str] = None

    @property
    def success(self) -> bool:
        return not self.validation_failures

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_failure(self, message: str) -> None:
        self.validation_failures.append(message)

    def add_generated_file(self, path: str) -> None:
        self.generated_files.append(path)
