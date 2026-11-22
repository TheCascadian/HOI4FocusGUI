"""Pure focus-tree validation.

This module answers one question: *what is wrong with this focus tree?* It
holds every structural, referential, and export-safety rule the editor knows
about, expressed against the domain dataclasses in :mod:`_dataStructs`.

Deliberate constraints, because they are what make the subsystem useful:

* **No Qt.** The validator runs in tests, in a headless script, and in a
  background job with no widget in sight. Presentation lives in
  :mod:`_diagnostics_panel`; rules live here.
* **No I/O by default.** Rules that need the filesystem (does this icon file
  exist?) only run when a :class:`ValidationContext` supplies the lookup
  tables, so the default path stays fast and reproducible.
* **Deterministic output.** Rules run in a fixed order and results are sorted
  by a total key, so the same tree always yields byte-identical diagnostics.
  Nothing is cached between runs: :func:`validate_focus_tree` builds its
  result from scratch, which is what keeps repeated validation of a large
  tree from accumulating stale findings.

Export safety is shared with the exporter rather than reimplemented: the
identifier, filename, and icon rules call straight into
:mod:`_export_validation`, so a diagnostic here and a hard failure there can
never disagree about what is writable.

See :data:`DIAGNOSTIC_CATALOG` for the code catalog and severity policy.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from _export_validation import (
    ExportValidationError,
    validate_focus_id,
    validate_icon_name,
)

__all__ = [
    "Severity",
    "SourceLocation",
    "Diagnostic",
    "ValidationContext",
    "ValidationResult",
    "DiagnosticCode",
    "DIAGNOSTIC_CATALOG",
    "validate_focus_tree",
    "iter_validate_focus_tree",
    "validation_rules",
]


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """How badly a finding compromises the tree.

    The policy is narrow on purpose, so "does this block export?" has exactly
    one answer:

    ``ERROR``
        The tree cannot be exported correctly. Either HOI4 would reject the
        output, or the exporter would have to invent data to proceed
        (dangling references, unwritable identifiers, cycles). Blocks export.
    ``WARNING``
        The tree exports, but the result is probably not what the author
        meant -- an unreachable focus, a one-sided mutual exclusion, two
        focuses stacked on the same coordinate. Never blocks export.
    ``INFO``
        Purely advisory polish; absent optional text, unused metadata.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Sort order: errors first, then warnings, then info."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLocation:
    """Where a finding came from in imported script, when that is known.

    Focuses created in the editor have no source location; focuses imported
    from a ``.txt`` do, and carrying it through means a diagnostic can point
    the user back at the original mod file.
    """

    filename: str
    line: int = 0
    column: int = 0

    def __str__(self) -> str:
        if self.line:
            return f"{self.filename}:{self.line}:{self.column}"
        return self.filename


@dataclass(frozen=True)
class Diagnostic:
    """One validation finding.

    Frozen because diagnostics are values, not records to be patched: a new
    validation run produces new diagnostics rather than mutating old ones.
    """

    code: str
    severity: Severity
    message: str
    object_id: Optional[str] = None
    field_path: str = ""
    suggestion: str = ""
    fix_id: Optional[str] = None
    location: Optional[SourceLocation] = None

    @property
    def blocks_export(self) -> bool:
        return self.severity is Severity.ERROR

    @property
    def sort_key(self) -> Tuple[Any, ...]:
        """Total order over diagnostics, so results are reproducible.

        Severity first (so the panel's default view leads with errors), then
        the object and field, then the code and message to break any
        remaining ties.
        """
        return (
            self.severity.rank,
            self.object_id or "",
            self.field_path,
            self.code,
            self.message,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain-data form, for logs, JSON reports, and CLI output."""
        data: Dict[str, Any] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "object_id": self.object_id,
            "field_path": self.field_path,
            "suggestion": self.suggestion,
            "fix_id": self.fix_id,
            "location": None,
        }
        if self.location is not None:
            data["location"] = {
                "filename": self.location.filename,
                "line": self.location.line,
                "column": self.location.column,
            }
        return data

    def format(self) -> str:
        """One-line human-readable rendering used by CLI and log output."""
        where = f" [{self.location}]" if self.location else ""
        target = f" ({self.object_id})" if self.object_id else ""
        line = f"{self.severity.value.upper()} {self.code}{target}{where}: {self.message}"
        if self.suggestion:
            line += f" -- {self.suggestion}"
        return line


# ---------------------------------------------------------------------------
# Diagnostic code catalog
# ---------------------------------------------------------------------------


class DiagnosticCode:
    """Stable diagnostic codes.

    These are part of the tool's contract: a code never changes meaning and
    never gets reused for something else. Retiring a rule means retiring its
    code, not recycling the number.
    """

    # Identity ------------------------------------------------------------
    FOCUS_ID_EMPTY = "FT001"
    FOCUS_ID_INVALID = "FT002"
    FOCUS_ID_DUPLICATE = "FT003"
    TREE_ID_INVALID = "FT004"

    # References ----------------------------------------------------------
    PREREQ_MISSING = "FT010"
    PREREQ_SELF = "FT011"
    PREREQ_DUPLICATE = "FT012"
    PREREQ_CYCLE = "FT013"
    MUTEX_MISSING = "FT014"
    MUTEX_SELF = "FT015"
    MUTEX_DUPLICATE = "FT016"
    MUTEX_ASYMMETRIC = "FT017"
    RELATIVE_POSITION_MISSING = "FT018"
    RELATIVE_POSITION_SELF = "FT019"
    RELATIVE_POSITION_CYCLE = "FT020"

    # Geometry ------------------------------------------------------------
    COORDINATE_INVALID = "FT030"
    COORDINATE_COLLISION = "FT031"

    # Scalars -------------------------------------------------------------
    COST_INVALID = "FT040"
    COST_ZERO = "FT041"

    # Presentation --------------------------------------------------------
    ICON_MISSING = "FT050"
    ICON_INVALID = "FT051"
    ICON_UNRESOLVED = "FT052"
    LOCALIZATION_MISSING_NAME = "FT053"
    LOCALIZATION_MISSING_DESCRIPTION = "FT054"

    # Structure -----------------------------------------------------------
    FOCUS_UNREACHABLE = "FT060"
    TREE_HAS_NO_ROOT = "FT061"

    # Export safety -------------------------------------------------------
    EXPORT_FILENAME_COLLISION = "FT070"
    # FT071 was a "derived filename is unwritable" check. It was removed
    # because the exporter's slug always falls back to a valid name, so the
    # rule could never fire. The code is retired, not reused.
    UNKNOWN_FIELD_UNEXPORTABLE = "FT072"


@dataclass(frozen=True)
class _CatalogEntry:
    """Documentation for one diagnostic code."""

    code: str
    severity: Severity
    title: str
    description: str


def _entry(code: str, severity: Severity, title: str, description: str) -> Tuple[str, _CatalogEntry]:
    return code, _CatalogEntry(code, severity, title, description)


DIAGNOSTIC_CATALOG: Dict[str, _CatalogEntry] = dict(
    [
        _entry(
            DiagnosticCode.FOCUS_ID_EMPTY,
            Severity.ERROR,
            "Focus has no ID",
            "A focus with a blank ID cannot be written to script or referenced by another focus.",
        ),
        _entry(
            DiagnosticCode.FOCUS_ID_INVALID,
            Severity.ERROR,
            "Focus ID is not a valid script identifier",
            "HOI4 focus IDs must be ASCII letters, digits, and underscores. The exporter refuses anything else.",
        ),
        _entry(
            DiagnosticCode.FOCUS_ID_DUPLICATE,
            Severity.ERROR,
            "Duplicate focus ID",
            "Two focuses share an ID, so every reference to it is ambiguous and the game keeps only one.",
        ),
        _entry(
            DiagnosticCode.TREE_ID_INVALID,
            Severity.ERROR,
            "Focus tree ID is not a valid script identifier",
            "The tree ID is written into the exported script and used to derive output filenames.",
        ),
        _entry(
            DiagnosticCode.PREREQ_MISSING,
            Severity.ERROR,
            "Prerequisite references an unknown focus",
            "The referenced focus does not exist in this tree, so the prerequisite can never be satisfied.",
        ),
        _entry(
            DiagnosticCode.PREREQ_SELF,
            Severity.ERROR,
            "Focus is its own prerequisite",
            "A focus that requires itself can never be taken.",
        ),
        _entry(
            DiagnosticCode.PREREQ_DUPLICATE,
            Severity.WARNING,
            "Duplicate prerequisite",
            "The same prerequisite is listed more than once. Harmless in game, but it exports duplicate blocks.",
        ),
        _entry(
            DiagnosticCode.PREREQ_CYCLE,
            Severity.ERROR,
            "Prerequisite cycle",
            "A group of focuses require each other in a loop, so none of them can ever be completed.",
        ),
        _entry(
            DiagnosticCode.MUTEX_MISSING,
            Severity.ERROR,
            "Mutually exclusive reference to an unknown focus",
            "The referenced focus does not exist in this tree.",
        ),
        _entry(
            DiagnosticCode.MUTEX_SELF,
            Severity.ERROR,
            "Focus is mutually exclusive with itself",
            "A focus that excludes itself can never be taken.",
        ),
        _entry(
            DiagnosticCode.MUTEX_DUPLICATE,
            Severity.WARNING,
            "Duplicate mutually exclusive reference",
            "The same exclusion is listed more than once.",
        ),
        _entry(
            DiagnosticCode.MUTEX_ASYMMETRIC,
            Severity.WARNING,
            "One-sided mutual exclusion",
            "Mutual exclusion is only enforced in the direction it is declared. The other focus does not exclude this one back.",
        ),
        _entry(
            DiagnosticCode.RELATIVE_POSITION_MISSING,
            Severity.ERROR,
            "relative_position_id references an unknown focus",
            "The focus would be positioned against a focus that does not exist, and HOI4 places it at the tree origin.",
        ),
        _entry(
            DiagnosticCode.RELATIVE_POSITION_SELF,
            Severity.ERROR,
            "Focus is positioned relative to itself",
            "Self-relative positioning is undefined.",
        ),
        _entry(
            DiagnosticCode.RELATIVE_POSITION_CYCLE,
            Severity.ERROR,
            "relative_position_id cycle",
            "A group of focuses position against each other in a loop, so no absolute position can be resolved.",
        ),
        _entry(
            DiagnosticCode.COORDINATE_INVALID,
            Severity.ERROR,
            "Focus coordinate is not a finite number",
            "NaN, infinity, or a non-numeric coordinate cannot be written to script.",
        ),
        _entry(
            DiagnosticCode.COORDINATE_COLLISION,
            Severity.WARNING,
            "Focuses share an exact coordinate",
            "Two or more focuses occupy the same grid cell and will render on top of each other.",
        ),
        _entry(
            DiagnosticCode.COST_INVALID,
            Severity.ERROR,
            "Focus cost is invalid",
            "Cost is in focus-weeks and must be a finite, non-negative number.",
        ),
        _entry(
            DiagnosticCode.COST_ZERO,
            Severity.WARNING,
            "Focus completes instantly",
            "A cost of zero makes the focus complete the moment it is selected, which is rarely intended.",
        ),
        _entry(
            DiagnosticCode.ICON_MISSING,
            Severity.WARNING,
            "Focus has no icon",
            "The focus exports without a sprite reference and shows the default placeholder in game.",
        ),
        _entry(
            DiagnosticCode.ICON_INVALID,
            Severity.WARNING,
            "Icon name cannot be exported",
            "The icon reference contains path separators or filesystem-reserved characters. "
            "The exporter skips such icons with a warning rather than failing, so the focus "
            "exports with a placeholder sprite. Warning, not error, to match that behaviour.",
        ),
        _entry(
            DiagnosticCode.ICON_UNRESOLVED,
            Severity.WARNING,
            "Icon file could not be found",
            "The icon is named but no matching file exists in the icon library or search paths, so nothing will be copied to the mod.",
        ),
        _entry(
            DiagnosticCode.LOCALIZATION_MISSING_NAME,
            Severity.WARNING,
            "Focus has no localized name",
            "The focus exports with its raw ID as the displayed name.",
        ),
        _entry(
            DiagnosticCode.LOCALIZATION_MISSING_DESCRIPTION,
            Severity.INFO,
            "Focus has no description",
            "The focus tooltip will be empty in game.",
        ),
        _entry(
            DiagnosticCode.FOCUS_UNREACHABLE,
            Severity.WARNING,
            "Focus can never be reached",
            "No chain of completable prerequisites leads to this focus, usually because a prerequisite is missing or caught in a cycle.",
        ),
        _entry(
            DiagnosticCode.TREE_HAS_NO_ROOT,
            Severity.WARNING,
            "Tree has no starting focus",
            "Every focus has at least one prerequisite, so nothing in the tree can be taken first.",
        ),
        _entry(
            DiagnosticCode.EXPORT_FILENAME_COLLISION,
            Severity.ERROR,
            "Two icons export to the same sprite",
            "Different icon references slugify to one sprite name, so one silently overwrites the "
            "other in goals.gfx. Error rather than warning because the exporter gives no "
            "indication at all that an authored icon was dropped.",
        ),
        _entry(
            DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE,
            Severity.WARNING,
            "Preserved script fragment may not survive export",
            "An unknown block captured during import has unbalanced braces or quotes and cannot be re-emitted safely.",
        ),
    ]
)


# ---------------------------------------------------------------------------
# Context and result
# ---------------------------------------------------------------------------


@dataclass
class ValidationContext:
    """Optional environment that lets extra rules run.

    Everything here is optional. With an empty context the validator is a
    pure function of the tree; supplying an icon library or search paths
    turns on the rules that need to know what exists on disk.
    """

    #: Icon key -> file path, as maintained by the editor's icon library.
    icon_library: Mapping[str, str] = field(default_factory=dict)
    #: Directories searched for icon files by basename, mirroring the exporter.
    icon_search_dirs: Sequence[str] = field(default_factory=tuple)
    #: Localization keys already defined elsewhere in the mod.
    known_localization_keys: Set[str] = field(default_factory=set)
    #: Source locations for imported focuses, keyed by focus ID.
    source_locations: Mapping[str, SourceLocation] = field(default_factory=dict)
    #: Whether to touch the filesystem at all. Off keeps validation pure.
    check_icon_files: bool = False


@dataclass
class ValidationResult:
    """The complete outcome of one validation run.

    Built fresh every run. Holding a result does not keep the validator
    alive or let old findings leak into the next run.
    """

    diagnostics: List[Diagnostic] = field(default_factory=list)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self.diagnostics)

    def __len__(self) -> int:
        return len(self.diagnostics)

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.WARNING]

    @property
    def infos(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.INFO]

    @property
    def blocks_export(self) -> bool:
        """True when at least one finding is severe enough to stop an export."""
        return any(d.blocks_export for d in self.diagnostics)

    def by_code(self, code: str) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.code == code]

    def for_focus(self, focus_id: str) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.object_id == focus_id]

    def counts(self) -> Dict[Severity, int]:
        return {
            Severity.ERROR: len(self.errors),
            Severity.WARNING: len(self.warnings),
            Severity.INFO: len(self.infos),
        }

    def format(self) -> str:
        """Multi-line report suitable for a terminal or a log file."""
        return "\n".join(d.format() for d in self.diagnostics)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _focuses_of(tree: Any) -> List[Any]:
    """Accept a FocusTree, a bare list of focuses, or anything with .focuses."""
    if tree is None:
        return []
    if isinstance(tree, (list, tuple)):
        return list(tree)
    return list(getattr(tree, "focuses", None) or [])


def _focus_id(focus: Any) -> str:
    value = getattr(focus, "id", None)
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _placeholder_id(index: int) -> str:
    """Identify a focus that has no usable ID of its own.

    Diagnostics still need a stable ``object_id`` so the panel can list them
    and sorting stays deterministic, so unnamed focuses are addressed by
    position.
    """
    return f"<focus #{index + 1}>"


def _finite_number(value: Any) -> Optional[float]:
    """Return ``value`` as a float when it is a finite number, else None.

    Booleans are rejected: ``True`` is technically an int, but a coordinate
    or cost of ``True`` is a data error, not the number 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _prerequisite_groups(focus: Any) -> List[List[str]]:
    """Normalise a focus's prerequisites into OR-groups that are AND-ed together.

    This mirrors exactly what :mod:`_exporter` writes:

    * ``prerequisites_groups`` (rich form) wins when present; each group is
      one ``prerequisite = { ... }`` block, i.e. an OR of its items.
    * ``prerequisites_grouped`` emits the flat list as a single OR block.
    * Otherwise each prerequisite becomes its own block, i.e. an AND.
    """
    groups = getattr(focus, "prerequisites_groups", None)
    if groups:
        normalised: List[List[str]] = []
        for group in groups:
            if isinstance(group, dict):
                items = group.get("items") or group.get("focus") or []
            else:
                items = group
            if isinstance(items, str):
                items = [items]
            normalised.append([str(item) for item in (items or []) if item])
        return [g for g in normalised if g]

    flat = [str(p) for p in (getattr(focus, "prerequisites", None) or []) if p]
    if not flat:
        return []
    if getattr(focus, "prerequisites_grouped", False):
        return [flat]
    return [[p] for p in flat]


# ---------------------------------------------------------------------------
# Rules
#
# Each rule is a generator over diagnostics. They are pure functions of
# (focuses, id_index, context) so they can be tested one at a time, and the
# UI can drive them incrementally without any of them holding state between
# runs.
# ---------------------------------------------------------------------------


def _rule_focus_ids(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Empty, malformed, and duplicated focus IDs."""
    seen: Dict[str, int] = {}
    for position, focus in enumerate(focuses):
        raw = _focus_id(focus)
        object_id = raw or _placeholder_id(position)
        location = ctx.source_locations.get(raw)
        path = f"focuses[{position}].id"

        if not raw.strip():
            yield Diagnostic(
                code=DiagnosticCode.FOCUS_ID_EMPTY,
                severity=Severity.ERROR,
                message="Focus has no ID.",
                object_id=object_id,
                field_path=path,
                suggestion="Give the focus a unique ID such as 'GER_rebuild_industry'.",
                fix_id="assign-generated-focus-id",
                location=location,
            )
            continue

        try:
            validate_focus_id(raw)
        except ExportValidationError as exc:
            yield Diagnostic(
                code=DiagnosticCode.FOCUS_ID_INVALID,
                severity=Severity.ERROR,
                message=f"Focus ID {raw!r} is not a valid script identifier: {exc.reason}.",
                object_id=object_id,
                field_path=path,
                suggestion="Use only ASCII letters, digits, and underscores.",
                fix_id="sanitize-focus-id",
                location=location,
            )

        first = seen.get(raw)
        if first is None:
            seen[raw] = position
        else:
            yield Diagnostic(
                code=DiagnosticCode.FOCUS_ID_DUPLICATE,
                severity=Severity.ERROR,
                message=(
                    f"Focus ID {raw!r} is used by more than one focus "
                    f"(also at position {first + 1})."
                ),
                object_id=object_id,
                field_path=path,
                suggestion="Rename one of the focuses; every reference to this ID is ambiguous.",
                fix_id="rename-duplicate-focus-id",
                location=location,
            )


def _rule_tree_identity(focuses, index, ctx, tree=None) -> Iterator[Diagnostic]:
    """Tree ID validity and the filename it produces."""
    if tree is None or isinstance(tree, (list, tuple)):
        return

    tree_id = getattr(tree, "id", "") or ""
    if not tree_id:
        return

    try:
        validate_focus_id(str(tree_id), field_name="tree id")
    except ExportValidationError as exc:
        yield Diagnostic(
            code=DiagnosticCode.TREE_ID_INVALID,
            severity=Severity.ERROR,
            message=f"Focus tree ID {tree_id!r} is not a valid script identifier: {exc.reason}.",
            object_id=str(tree_id),
            field_path="tree.id",
            suggestion="Use only ASCII letters, digits, and underscores.",
            fix_id="sanitize-tree-id",
        )


def _rule_prerequisites(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Missing, self, and duplicated prerequisite references."""
    for position, focus in enumerate(focuses):
        focus_id = _focus_id(focus) or _placeholder_id(position)
        location = ctx.source_locations.get(_focus_id(focus))
        seen: Set[str] = set()

        for slot, prereq in enumerate(getattr(focus, "prerequisites", None) or []):
            prereq = str(prereq)
            path = f"focuses[{focus_id}].prerequisites[{slot}]"

            if prereq == _focus_id(focus) and prereq:
                yield Diagnostic(
                    code=DiagnosticCode.PREREQ_SELF,
                    severity=Severity.ERROR,
                    message=f"Focus {focus_id!r} lists itself as a prerequisite.",
                    object_id=focus_id,
                    field_path=path,
                    suggestion="Remove the self-reference; it makes the focus impossible to take.",
                    fix_id="remove-prerequisite",
                    location=location,
                )
            elif prereq not in index:
                yield Diagnostic(
                    code=DiagnosticCode.PREREQ_MISSING,
                    severity=Severity.ERROR,
                    message=f"Prerequisite {prereq!r} does not exist in this tree.",
                    object_id=focus_id,
                    field_path=path,
                    suggestion=f"Create a focus with ID {prereq!r}, or remove the prerequisite.",
                    fix_id="remove-prerequisite",
                    location=location,
                )

            if prereq in seen:
                yield Diagnostic(
                    code=DiagnosticCode.PREREQ_DUPLICATE,
                    severity=Severity.WARNING,
                    message=f"Prerequisite {prereq!r} is listed more than once.",
                    object_id=focus_id,
                    field_path=path,
                    suggestion="Remove the duplicate entry.",
                    fix_id="remove-duplicate-prerequisite",
                    location=location,
                )
            seen.add(prereq)


def _rule_mutually_exclusive(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Missing, self, duplicated, and one-sided mutual exclusions."""
    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)
        seen: Set[str] = set()

        for slot, other_id in enumerate(getattr(focus, "mutually_exclusive", None) or []):
            other_id = str(other_id)
            path = f"focuses[{focus_id}].mutually_exclusive[{slot}]"

            if other_id and other_id == raw_id:
                yield Diagnostic(
                    code=DiagnosticCode.MUTEX_SELF,
                    severity=Severity.ERROR,
                    message=f"Focus {focus_id!r} is mutually exclusive with itself.",
                    object_id=focus_id,
                    field_path=path,
                    suggestion="Remove the self-reference.",
                    fix_id="remove-mutually-exclusive",
                    location=location,
                )
            elif other_id not in index:
                yield Diagnostic(
                    code=DiagnosticCode.MUTEX_MISSING,
                    severity=Severity.ERROR,
                    message=f"Mutually exclusive focus {other_id!r} does not exist in this tree.",
                    object_id=focus_id,
                    field_path=path,
                    suggestion=f"Create a focus with ID {other_id!r}, or remove the exclusion.",
                    fix_id="remove-mutually-exclusive",
                    location=location,
                )
            else:
                other = index[other_id]
                reciprocal = [str(m) for m in (getattr(other, "mutually_exclusive", None) or [])]
                if raw_id not in reciprocal:
                    yield Diagnostic(
                        code=DiagnosticCode.MUTEX_ASYMMETRIC,
                        severity=Severity.WARNING,
                        message=(
                            f"Focus {focus_id!r} excludes {other_id!r}, but {other_id!r} "
                            f"does not exclude {focus_id!r} back."
                        ),
                        object_id=focus_id,
                        field_path=path,
                        suggestion=f"Add {focus_id!r} to the mutually exclusive list of {other_id!r}.",
                        fix_id="add-reciprocal-mutually-exclusive",
                        location=location,
                    )

            if other_id in seen:
                yield Diagnostic(
                    code=DiagnosticCode.MUTEX_DUPLICATE,
                    severity=Severity.WARNING,
                    message=f"Mutually exclusive focus {other_id!r} is listed more than once.",
                    object_id=focus_id,
                    field_path=path,
                    suggestion="Remove the duplicate entry.",
                    fix_id="remove-duplicate-mutually-exclusive",
                    location=location,
                )
            seen.add(other_id)


def _rule_relative_position(focuses, index, ctx) -> Iterator[Diagnostic]:
    """relative_position_id references: missing, self, and cyclic."""
    edges: Dict[str, str] = {}

    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)
        target = getattr(focus, "relative_position_id", None)
        if not target:
            continue
        target = str(target)
        path = f"focuses[{focus_id}].relative_position_id"

        if target == raw_id:
            yield Diagnostic(
                code=DiagnosticCode.RELATIVE_POSITION_SELF,
                severity=Severity.ERROR,
                message=f"Focus {focus_id!r} is positioned relative to itself.",
                object_id=focus_id,
                field_path=path,
                suggestion="Clear relative_position_id to use absolute coordinates.",
                fix_id="clear-relative-position",
                location=location,
            )
        elif target not in index:
            yield Diagnostic(
                code=DiagnosticCode.RELATIVE_POSITION_MISSING,
                severity=Severity.ERROR,
                message=f"relative_position_id {target!r} does not exist in this tree.",
                object_id=focus_id,
                field_path=path,
                suggestion="Point at an existing focus, or clear relative_position_id.",
                fix_id="clear-relative-position",
                location=location,
            )
        elif raw_id:
            edges[raw_id] = target

    for cycle in _find_cycles({k: [v] for k, v in edges.items()}):
        chain = " -> ".join(cycle + [cycle[0]])
        for member in cycle:
            yield Diagnostic(
                code=DiagnosticCode.RELATIVE_POSITION_CYCLE,
                severity=Severity.ERROR,
                message=f"relative_position_id cycle: {chain}.",
                object_id=member,
                field_path=f"focuses[{member}].relative_position_id",
                suggestion="Break the loop by giving one focus in it an absolute position.",
                fix_id="clear-relative-position",
                location=ctx.source_locations.get(member),
            )


def _rule_prerequisite_cycles(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Loops in the prerequisite graph.

    Self-prerequisites are reported by :func:`_rule_prerequisites` and
    excluded here, so a focus requiring itself yields one precise diagnostic
    rather than two overlapping ones.
    """
    graph: Dict[str, List[str]] = {}
    for focus in focuses:
        focus_id = _focus_id(focus)
        if not focus_id:
            continue
        targets = [
            str(p)
            for p in (getattr(focus, "prerequisites", None) or [])
            if str(p) in index and str(p) != focus_id
        ]
        graph[focus_id] = targets

    for cycle in _find_cycles(graph):
        # Edges run focus -> prerequisite, so spell the direction out rather
        # than leaving an arrow the reader has to guess at.
        chain = " requires ".join(cycle + [cycle[0]])
        for member in cycle:
            yield Diagnostic(
                code=DiagnosticCode.PREREQ_CYCLE,
                severity=Severity.ERROR,
                message=f"Prerequisite cycle: {chain}.",
                object_id=member,
                field_path=f"focuses[{member}].prerequisites",
                suggestion="Remove one prerequisite in the loop; none of these focuses can be completed.",
                fix_id="remove-prerequisite",
                location=ctx.source_locations.get(member),
            )


def _rule_coordinates(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Non-finite coordinates and exact overlaps."""
    occupied: Dict[Tuple[float, float, Optional[str]], List[str]] = {}

    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)

        coords: Dict[str, Optional[float]] = {}
        for axis in ("x", "y"):
            value = getattr(focus, axis, None)
            number = _finite_number(value)
            coords[axis] = number
            if number is None:
                yield Diagnostic(
                    code=DiagnosticCode.COORDINATE_INVALID,
                    severity=Severity.ERROR,
                    message=f"Coordinate {axis} is {value!r}, which is not a finite number.",
                    object_id=focus_id,
                    field_path=f"focuses[{focus_id}].{axis}",
                    suggestion="Set the coordinate to a whole number of grid cells.",
                    fix_id="reset-coordinate",
                    location=location,
                )

        if coords["x"] is None or coords["y"] is None:
            continue

        # Focuses positioned relative to different anchors can legitimately
        # share an offset, so the collision key includes the anchor.
        anchor = getattr(focus, "relative_position_id", None)
        key = (coords["x"], coords["y"], str(anchor) if anchor else None)
        occupied.setdefault(key, []).append(focus_id)

    for (x, y, anchor), ids in occupied.items():
        if len(ids) < 2:
            continue
        where = f"({x:g}, {y:g})"
        if anchor:
            where += f" relative to {anchor!r}"
        others = ", ".join(repr(i) for i in ids)
        for focus_id in ids:
            yield Diagnostic(
                code=DiagnosticCode.COORDINATE_COLLISION,
                severity=Severity.WARNING,
                message=f"{len(ids)} focuses share the position {where}: {others}.",
                object_id=focus_id,
                field_path=f"focuses[{focus_id}].x",
                suggestion="Move one of the focuses so they do not render on top of each other.",
                fix_id="nudge-coordinate",
                location=ctx.source_locations.get(focus_id),
            )


def _rule_cost(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Completion cost sanity."""
    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)
        path = f"focuses[{focus_id}].cost"

        value = getattr(focus, "cost", None)
        number = _finite_number(value)

        if number is None:
            yield Diagnostic(
                code=DiagnosticCode.COST_INVALID,
                severity=Severity.ERROR,
                message=f"Cost is {value!r}, which is not a finite number.",
                object_id=focus_id,
                field_path=path,
                suggestion="Set cost to a number of focus-weeks, e.g. 10.",
                fix_id="reset-cost",
                location=location,
            )
        elif number < 0:
            yield Diagnostic(
                code=DiagnosticCode.COST_INVALID,
                severity=Severity.ERROR,
                message=f"Cost is negative ({number:g}).",
                object_id=focus_id,
                field_path=path,
                suggestion="Set cost to zero or more focus-weeks.",
                fix_id="reset-cost",
                location=location,
            )
        elif number == 0:
            yield Diagnostic(
                code=DiagnosticCode.COST_ZERO,
                severity=Severity.WARNING,
                message="Cost is zero, so the focus completes instantly.",
                object_id=focus_id,
                field_path=path,
                suggestion="Set a non-zero cost unless the focus is deliberately instant.",
                fix_id="reset-cost",
                location=location,
            )


def _rule_icons(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Icon presence, export safety, and resolvability."""
    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)
        path = f"focuses[{focus_id}].icon"
        icon = getattr(focus, "icon", None)

        if not icon or not str(icon).strip():
            yield Diagnostic(
                code=DiagnosticCode.ICON_MISSING,
                severity=Severity.WARNING,
                message="Focus has no icon.",
                object_id=focus_id,
                field_path=path,
                suggestion="Pick an icon so the focus does not render as a placeholder.",
                fix_id="assign-default-icon",
                location=location,
            )
            continue

        icon = str(icon)
        try:
            validate_icon_name(icon)
        except ExportValidationError as exc:
            yield Diagnostic(
                code=DiagnosticCode.ICON_INVALID,
                severity=Severity.WARNING,
                message=f"Icon {icon!r} cannot be exported: {exc.reason}.",
                object_id=focus_id,
                field_path=path,
                suggestion="Reference the icon by its sprite name or plain filename.",
                fix_id="sanitize-icon-name",
                location=location,
            )
            continue

        if ctx.check_icon_files and not _icon_resolves(icon, ctx):
            yield Diagnostic(
                code=DiagnosticCode.ICON_UNRESOLVED,
                severity=Severity.WARNING,
                message=f"No file found for icon {icon!r}.",
                object_id=focus_id,
                field_path=path,
                suggestion="Add the icon file to the library or an icon search directory.",
                fix_id=None,
                location=location,
            )


def _icon_resolves(icon: str, ctx: ValidationContext) -> bool:
    """Whether an icon reference maps to a real file, exporter-style.

    Sprite names (``GFX_...``) are assumed to be provided by the base game or
    another mod, so they are never reported as unresolved.
    """
    if icon.upper().startswith("GFX_"):
        return True
    if icon in ctx.icon_library:
        return True
    basename = os.path.basename(icon)
    if os.path.isabs(icon) and os.path.exists(icon):
        return True
    for directory in ctx.icon_search_dirs:
        if os.path.exists(os.path.join(directory, basename)):
            return True
    return False


def _rule_localization(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Missing display name and description text."""
    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)

        name = (getattr(focus, "name", "") or "").strip()
        if not name and raw_id not in ctx.known_localization_keys:
            yield Diagnostic(
                code=DiagnosticCode.LOCALIZATION_MISSING_NAME,
                severity=Severity.WARNING,
                message="Focus has no localized name.",
                object_id=focus_id,
                field_path=f"focuses[{focus_id}].name",
                suggestion="Give the focus a display name; otherwise the raw ID is shown in game.",
                fix_id="copy-id-to-name",
                location=location,
            )

        description = (getattr(focus, "description", "") or "").strip()
        if not description:
            yield Diagnostic(
                code=DiagnosticCode.LOCALIZATION_MISSING_DESCRIPTION,
                severity=Severity.INFO,
                message="Focus has no description.",
                object_id=focus_id,
                field_path=f"focuses[{focus_id}].description",
                suggestion="Add tooltip text describing what the focus does.",
                fix_id=None,
                location=location,
            )


def _rule_reachability(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Focuses no chain of satisfiable prerequisites can reach.

    Reachability is computed with the same AND/OR semantics the exporter
    writes: a focus is reachable when every one of its prerequisite blocks
    has at least one reachable member. Fixed-point iteration handles cycles
    naturally -- a focus caught in a loop never becomes reachable.
    """
    reachable: Set[str] = set()
    pending = [f for f in focuses if _focus_id(f)]
    roots_exist = False

    changed = True
    while changed:
        changed = False
        for focus in pending:
            focus_id = _focus_id(focus)
            if focus_id in reachable:
                continue
            groups = _prerequisite_groups(focus)
            if not groups:
                reachable.add(focus_id)
                changed = True
                continue
            if all(any(item in reachable for item in group) for group in groups):
                reachable.add(focus_id)
                changed = True

    for focus in pending:
        if not _prerequisite_groups(focus):
            roots_exist = True
            break

    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        if not raw_id or raw_id in reachable:
            continue
        yield Diagnostic(
            code=DiagnosticCode.FOCUS_UNREACHABLE,
            severity=Severity.WARNING,
            message=f"Focus {raw_id!r} can never be reached from any starting focus.",
            object_id=raw_id,
            field_path=f"focuses[{raw_id}].prerequisites",
            suggestion="Check its prerequisites for missing focuses or a cycle.",
            fix_id=None,
            location=ctx.source_locations.get(raw_id),
        )

    if pending and not roots_exist:
        yield Diagnostic(
            code=DiagnosticCode.TREE_HAS_NO_ROOT,
            severity=Severity.WARNING,
            message="Every focus in the tree has a prerequisite, so none can be taken first.",
            object_id=None,
            field_path="tree.focuses",
            suggestion="Remove the prerequisites from at least one focus to make it a starting focus.",
            fix_id=None,
        )


def _rule_export_filename_collisions(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Distinct icon references that would collapse into one sprite on export.

    The exporter turns each icon reference into a sprite name by slugifying
    its stem, then builds ``goals.gfx`` from a dict keyed by that name. Two
    references that slugify the same way -- ``flag.png`` and ``flag.dds``,
    or ``my icon.png`` and ``my_icon.png`` -- collapse into a single entry
    and whichever was processed last silently wins. Nothing downstream
    notices, which is exactly why it belongs here.
    """
    by_sprite: Dict[str, Dict[str, List[str]]] = {}

    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        icon = getattr(focus, "icon", None)
        if not icon:
            continue
        icon = str(icon).strip()
        if icon.upper().startswith("GFX_"):
            # A sprite reference names an existing sprite. Nothing is
            # derived and nothing is copied, so nothing can collide.
            continue
        stem = os.path.splitext(os.path.basename(icon))[0]
        sprite = _slugify_like_exporter(stem, stem)
        if not sprite:
            continue
        by_sprite.setdefault(sprite, {}).setdefault(icon, []).append(focus_id)

    for sprite, sources in sorted(by_sprite.items()):
        if len(sources) < 2:
            continue
        listed = ", ".join(repr(src) for src in sorted(sources))
        for source in sorted(sources):
            for focus_id in sources[source]:
                yield Diagnostic(
                    code=DiagnosticCode.EXPORT_FILENAME_COLLISION,
                    severity=Severity.ERROR,
                    message=(
                        f"Icon {source!r} exports as sprite {sprite!r}, which "
                        f"{len(sources)} different icons claim: {listed}. "
                        "Only one of them survives the export."
                    ),
                    object_id=focus_id,
                    field_path=f"focuses[{focus_id}].icon",
                    suggestion="Rename one of the icon files so each produces its own sprite name.",
                    fix_id=None,
                    location=ctx.source_locations.get(focus_id),
                )


def _slugify_like_exporter(value: str, fallback: str) -> str:
    """Reproduce the exporter's sprite/filename slug.

    A local copy rather than an import: ``_exporter`` pulls in the whole
    writer stack, and this module stays cheap enough to import from a
    script. :func:`test_slug_matches_the_exporter` keeps the two honest.
    """
    candidate = str(value or "").strip() or str(fallback or "").strip()
    candidate = re.sub(r"[^0-9A-Za-z_\-]+", "_", candidate).strip("_")
    return candidate


def _rule_unknown_fields(focuses, index, ctx) -> Iterator[Diagnostic]:
    """Preserved script fragments that cannot be re-emitted safely.

    Import keeps unknown blocks verbatim in ``raw_unparsed`` so they survive a
    round trip. A fragment with unbalanced braces or an unterminated string
    would corrupt everything after it in the exported file, so it is flagged
    rather than written blindly.
    """
    for position, focus in enumerate(focuses):
        raw_id = _focus_id(focus)
        focus_id = raw_id or _placeholder_id(position)
        location = ctx.source_locations.get(raw_id)

        for slot, chunk in enumerate(getattr(focus, "raw_unparsed", None) or []):
            problem = _unbalanced_reason(str(chunk))
            if problem is None:
                continue
            yield Diagnostic(
                code=DiagnosticCode.UNKNOWN_FIELD_UNEXPORTABLE,
                severity=Severity.WARNING,
                message=f"Preserved script fragment cannot be exported safely: {problem}.",
                object_id=focus_id,
                field_path=f"focuses[{focus_id}].raw_unparsed[{slot}]",
                suggestion="Fix the fragment in the focus's raw script, or delete it.",
                fix_id=None,
                location=location,
            )


def _unbalanced_reason(text: str) -> Optional[str]:
    """Describe why a raw fragment is unsafe to re-emit, or None if it is fine.

    A deliberately small scan rather than a full parse: braces and quotes are
    what break the *surrounding* file, and everything else in an unknown
    block is the mod author's business.
    """
    depth = 0
    in_string = False
    in_comment = False
    escaped = False

    for char in text:
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == "#":
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return "a closing brace with no matching opening brace"

    if in_string:
        return "an unterminated quoted string"
    if depth > 0:
        return f"{depth} unclosed block{'s' if depth > 1 else ''}"
    return None


#: Rules in execution order. Order affects nothing but the incremental
#: reporting sequence, since results are sorted before they are returned.
_RULES: List[Callable[..., Iterator[Diagnostic]]] = [
    _rule_focus_ids,
    _rule_prerequisites,
    _rule_mutually_exclusive,
    _rule_relative_position,
    _rule_prerequisite_cycles,
    _rule_coordinates,
    _rule_cost,
    _rule_icons,
    _rule_localization,
    _rule_reachability,
    _rule_export_filename_collisions,
    _rule_unknown_fields,
]


def validation_rules() -> List[str]:
    """Names of the registered rules, for diagnostics about the validator itself."""
    return [rule.__name__ for rule in _RULES]


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def _find_cycles(graph: Mapping[str, Iterable[str]]) -> List[List[str]]:
    """Return every elementary cycle in a directed graph, deterministically.

    Iterative Tarjan strongly-connected-components followed by a walk of each
    non-trivial component. Iterative rather than recursive because a deep
    prerequisite chain in a large mod would otherwise blow the Python stack,
    and validating a tree must never crash the editor.

    Each cycle is rotated to start at its smallest member and the list is
    sorted, so the same graph always produces the same cycles in the same
    order.
    """
    nodes = sorted(graph.keys())
    successors = {node: sorted(set(graph.get(node, ()))) for node in nodes}

    index_of: Dict[str, int] = {}
    low_link: Dict[str, int] = {}
    on_stack: Dict[str, bool] = {}
    stack: List[str] = []
    counter = 0
    components: List[List[str]] = []

    for root in nodes:
        if root in index_of:
            continue
        # (node, iterator position) frames, walked explicitly.
        work: List[Tuple[str, int]] = [(root, 0)]
        index_of[root] = low_link[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True

        while work:
            node, next_child = work[-1]
            children = successors.get(node, [])

            if next_child < len(children):
                work[-1] = (node, next_child + 1)
                child = children[next_child]
                if child not in index_of:
                    index_of[child] = low_link[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack[child] = True
                    work.append((child, 0))
                elif on_stack.get(child):
                    low_link[node] = min(low_link[node], index_of[child])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                low_link[parent] = min(low_link[parent], low_link[node])

            if low_link[node] == index_of[node]:
                component: List[str] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
                elif node in successors.get(node, []):
                    components.append([node])

    cycles: List[List[str]] = []
    for component in components:
        cycles.append(_walk_component(component, successors))
    cycles.sort()
    return cycles


def _walk_component(component: Sequence[str], successors: Mapping[str, Sequence[str]]) -> List[str]:
    """Produce one representative cycle through a strongly connected component.

    Reporting every elementary cycle in a dense component is exponential and
    unhelpful; one concrete loop through it tells the user what to break.
    """
    members = set(component)
    start = min(component)
    path = [start]
    seen = {start}
    current = start

    while True:
        nxt = None
        for candidate in successors.get(current, []):
            if candidate not in members:
                continue
            if candidate == start and len(path) > 1:
                return path
            if candidate not in seen:
                nxt = candidate
                break
        if nxt is None:
            # Single-node component with a self-edge, or a dead end inside
            # the component; the path so far is the cycle we can name.
            return path
        path.append(nxt)
        seen.add(nxt)
        current = nxt


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def iter_validate_focus_tree(
    tree: Any,
    context: Optional[ValidationContext] = None,
) -> Iterator[Diagnostic]:
    """Yield diagnostics as each rule produces them.

    This is what lets the UI validate a large tree without freezing: the
    caller can consume a slice, return to the event loop, and resume. The
    stream is *not* sorted -- use :func:`validate_focus_tree` when you want
    the deterministic ordering.
    """
    ctx = context or ValidationContext()
    focuses = _focuses_of(tree)
    index = {}
    for focus in focuses:
        focus_id = _focus_id(focus)
        if focus_id:
            index.setdefault(focus_id, focus)

    yield from _rule_tree_identity(focuses, index, ctx, tree=tree)
    for rule in _RULES:
        yield from rule(focuses, index, ctx)


def validate_focus_tree(
    tree: Any,
    context: Optional[ValidationContext] = None,
) -> ValidationResult:
    """Validate a focus tree and return a sorted, self-contained result.

    Accepts a :class:`~_dataStructs.FocusTree`, a plain list of focuses, or
    any object exposing a ``focuses`` attribute, which keeps the validator
    usable from a script that never constructs a full tree.
    """
    diagnostics = sorted(iter_validate_focus_tree(tree, context), key=lambda d: d.sort_key)
    return ValidationResult(diagnostics=diagnostics)
