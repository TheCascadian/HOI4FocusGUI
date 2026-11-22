# region File (auto-generated)
# endregion

# _txt_converter.py
"""Semantic mapper from parsed Paradox script to the project's focus dicts.

The lexical and syntactic work lives in :mod:`_paradox_lexer` and
:mod:`_paradox_cst`; this module only walks the resulting concrete syntax tree
and pulls out the focus-tree fields the editor understands. The CST is *not*
consumed or mutated in the process -- it stays available on
:attr:`TxtFocusParser.document`, and every focus dict keeps the verbatim source
of its block in ``raw``, so unknown constructs survive a round trip.

Qt is never imported here (directly or transitively): importing this module
costs the standard library plus the two parser modules, nothing else.

Public API (unchanged from the previous regex-based implementation):
    TxtFocusParser(content).parse() -> dict
    convert_txt_to_project_dict(content) -> dict

Both now accept ``filename`` (used in diagnostics) and ``recover``. With
``recover=False`` structurally invalid input raises
:class:`~_paradox_lexer.ParadoxSyntaxError` rather than silently yielding a
half-parsed tree; with the default ``recover=True`` parsing continues and the
returned dict carries a ``diagnostics`` list of JSON-serializable records.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, Union

from _paradox_cst import Assignment, Block, Document, Parser, Scalar
from _paradox_lexer import Diagnostic, ParadoxSyntaxError, SourceText

__all__ = [
    "TxtFocusParser",
    "convert_txt_to_project_dict",
    "ParadoxSyntaxError",
]

#: Focus keys whose value is a block we understand. Anything else that maps to a
#: block is recorded in ``raw_unparsed`` (and stripped from ``clean_raw``) so the
#: importer can offer to cull it -- but it is never dropped from ``raw``.
KNOWN_FOCUS_BLOCK_KEYS = frozenset(
    {
        "ai_will_do",
        "allow_branch",
        "available",
        "bypass",
        "cancel",
        "complete_tooltip",
        "completion_reward",
        "icon",
        "mutually_exclusive",
        "option",
        "options",
        "prerequisite",
        "prerequisites",
        "remove_effect",
        "search_filters",
        "select_effect",
        "trigger",
        "visible",
        "will_lead_to_war_with",
    }
)

#: Focus fields kept as the verbatim inner text of their block, exactly as the
#: editor's free-text panes expect. A repeated key yields a list of strings.
NESTED_BLOCK_FIELDS: Tuple[str, ...] = (
    "ai_will_do",
    "completion_reward",
    "select_effect",
    "remove_effect",
    "available",
    "bypass",
    "cancel",
    "allow_branch",
    "visible",
)

#: Trigger keys the GUI can simulate, mapped to how their value is normalized.
_CONDITION_KEYS: Tuple[str, ...] = (
    "has_completed_focus",
    "has_government",
    "has_dlc",
)

_BOOLEAN_FOCUS_FLAGS: Tuple[str, ...] = (
    "cancel_if_invalid",
    "continue_if_invalid",
    "available_if_capitulated",
)


# =============================================================================
# Small CST helpers
# =============================================================================

def _scalar_value(block: Block, key: str) -> Optional[str]:
    """Decoded text of the first scalar assignment named ``key``."""
    scalar = block.scalar(key)
    return scalar.value if scalar is not None else None


def _number_value(block: Block, key: str) -> Optional[Union[int, float]]:
    scalar = block.scalar(key)
    return scalar.as_number() if scalar is not None else None


def _bool_value(block: Block, key: str) -> Optional[bool]:
    scalar = block.scalar(key)
    return scalar.as_bool() if scalar is not None else None


def _int_or(value: Optional[Union[int, float]], default: int) -> int:
    if value is None:
        return default
    return int(value)


def _dedup(items: List[str]) -> List[str]:
    """Order-preserving de-duplication; parse output must be deterministic."""
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _focus_ids(container, recursive: bool = True) -> List[str]:
    """Decoded ids of ``focus = <id>`` assignments inside ``container``."""
    source = container.find_all("focus") if recursive else container.get_all("focus")
    return _dedup(
        [a.value.value.strip() for a in source if isinstance(a.value, Scalar)]
    )


def _conditions_sorted(container, where: str) -> List[Dict[str, str]]:
    """Extract the handful of trigger types the GUI can evaluate.

    ``find_all`` runs once per condition key, so the hits are re-sorted by source
    offset to keep the result in file order rather than key order.
    """
    pairs: List[Tuple[int, Dict[str, str]]] = []
    for key in _CONDITION_KEYS:
        for assignment in container.find_all(key):
            if not isinstance(assignment.value, Scalar):
                continue
            value = assignment.value.value.strip()
            if not value:
                continue
            if key == "has_government":
                value = value.lower()
            pairs.append((assignment.start, {"where": where, "type": key, "value": value}))
    pairs.sort(key=lambda pair: pair[0])
    return [entry for _, entry in pairs]


# =============================================================================
# Parser
# =============================================================================

class TxtFocusParser:
    """Parse HOI4 focus script into the project's focus/tree dictionaries.

    ``parse()`` returns the same shape it always has -- ``focuses``,
    ``tree_info`` and ``raw`` -- plus a ``diagnostics`` list. The concrete syntax
    tree behind the result stays reachable via :attr:`document`, and
    :attr:`focus_nodes` lines up one-to-one with ``result['focuses']`` for
    callers that want the syntax nodes rather than the flattened dicts.
    """

    def __init__(
        self,
        content: str,
        filename: str = "<string>",
        recover: bool = True,
    ) -> None:
        self.content = content
        self.filename = filename
        self.recover = recover
        self.source = SourceText(content, filename)
        self.document: Optional[Document] = None
        self.diagnostics: List[Diagnostic] = []
        #: CST blocks aligned by index with ``parse()['focuses']``.
        self.focus_nodes: List[Block] = []

    # -------------------------
    # High-level parse entry
    # -------------------------
    def parse(self) -> Dict[str, Any]:
        """Parse the whole document. Raises in strict mode, reports otherwise."""
        document = Parser(self.source, recover=self.recover).parse()
        self.document = document
        self.diagnostics = list(document.diagnostics)
        self.focus_nodes = []

        result: Dict[str, Any] = {
            "focuses": [],
            "tree_info": {},
            "raw": self.content,
            "diagnostics": [],
        }

        tree_block = document.block("focus_tree")
        if tree_block is not None:
            tree_assignment = document.get("focus_tree")
            result["tree_info"] = self._map_tree_info(tree_block, tree_assignment)

        for assignment in self._collect_focus_assignments(document):
            focus = self._map_focus(assignment)
            if focus is None:
                continue
            result["focuses"].append(focus)
            self.focus_nodes.append(assignment.value)

        self._resolve_relative_positions(result["focuses"])

        result["diagnostics"] = [d.to_dict() for d in self.diagnostics]
        return result

    # -------------------------
    # Focus discovery
    # -------------------------
    def _collect_focus_assignments(self, document: Document) -> List[Assignment]:
        """Find every focus definition, wherever it sits in the file.

        Focuses normally live inside ``focus_tree``, but mod files also declare
        them at top level, and ``shared_focus = { ... }`` is a focus definition
        too (as opposed to ``shared_focus = some_id``, which is a tree-level
        pointer and is handled in :meth:`_map_tree_info`).
        """
        found: List[Assignment] = []
        for node in document.walk():
            if not isinstance(node, Assignment):
                continue
            if not isinstance(node.value, Block):
                continue
            if node.key.value.lower() in ("focus", "shared_focus"):
                found.append(node)
        found.sort(key=lambda assignment: assignment.start)
        return found

    # -------------------------
    # Tree-level mapping
    # -------------------------
    def _map_tree_info(
        self, block: Block, assignment: Optional[Assignment]
    ) -> Dict[str, Any]:
        shared = block.get("shared_focus")
        tree_info: Dict[str, Any] = {
            "id": _scalar_value(block, "id"),
            "country": [],
            "reset_on_civil_war": _bool_value(block, "reset_on_civil_war"),
            # Only a scalar shared_focus names another tree; a block is a focus.
            "shared_focus": (
                shared.value.value
                if shared is not None and isinstance(shared.value, Scalar)
                else None
            ),
            "continuous_focus_position": self._coordinates(
                block.block("continuous_focus_position")
            ),
            "default": _bool_value(block, "default"),
            "initial_show_position": self._coordinates(
                block.block("initial_show_position")
            ),
            "raw": assignment.text if assignment is not None else block.text,
        }

        tags: List[str] = []
        for country_block in block.blocks("country"):
            for tag_assignment in country_block.find_all("tag"):
                if isinstance(tag_assignment.value, Scalar):
                    tag = tag_assignment.value.value.strip()
                    if tag:
                        tags.append(tag.upper())
        tree_info["country"] = _dedup(tags)

        return tree_info

    def _coordinates(self, block: Optional[Block]) -> Optional[Dict[str, int]]:
        if block is None:
            return None
        coords: Dict[str, int] = {}
        for axis in ("x", "y"):
            value = _number_value(block, axis)
            if value is not None:
                coords[axis] = int(value)
        return coords or None

    # -------------------------
    # Focus-level mapping
    # -------------------------
    def _map_focus(self, assignment: Assignment) -> Optional[Dict[str, Any]]:
        block = assignment.value
        assert isinstance(block, Block)  # guaranteed by _collect_focus_assignments

        focus_id = _scalar_value(block, "id")
        name = _scalar_value(block, "text")
        if name is None:
            name = _scalar_value(block, "name")

        if not focus_id and not name:
            self._report(
                node=assignment,
                code="focus-without-id",
                message="focus block has neither an 'id' nor a 'name'",
                expected="id = <focus_id>",
                severity="warning",
            )
            return None

        prereq_flat, prereq_groups = self._map_prerequisites(block)
        unparsed, clean_raw = self._split_unknown_blocks(assignment, block)

        focus: Dict[str, Any] = {
            "id": focus_id,
            "name": name if name is not None else (focus_id or ""),
            "cost": _int_or(_number_value(block, "cost"), 10),
            "icon": self._map_icon(block),
            "x": _int_or(_number_value(block, "x"), 0),
            "y": _int_or(_number_value(block, "y"), 0),
            "relative_position_id": _scalar_value(block, "relative_position_id"),
            "prerequisites": prereq_flat,
            "prerequisites_groups": prereq_groups,
            "mutually_exclusive": self._map_mutually_exclusive(block),
            "description": None,
            "search_filters": self._map_search_filters(block),
            "will_lead_to_war_with": self._map_value_list(block, "will_lead_to_war_with"),
            "raw": assignment.text,
            "raw_unparsed": unparsed,
            "has_unparsed": bool(unparsed),
            "clean_raw": clean_raw,
        }

        for flag in _BOOLEAN_FOCUS_FLAGS:
            focus[flag] = _bool_value(block, flag)

        for key in NESTED_BLOCK_FIELDS:
            texts = [b.inner_text for b in block.blocks(key)]
            if not texts:
                focus[key] = None
            elif len(texts) == 1:
                focus[key] = texts[0]
            else:
                focus[key] = texts

        focus.update(self._map_visibility(block))
        focus["avail_conditions"] = self._map_conditions(block)

        return focus

    # -------------------------
    # Focus field mappers
    # -------------------------
    def _map_icon(self, block: Block) -> Optional[Union[str, List[Dict[str, Optional[str]]]]]:
        """Map ``icon``, which is either a bare value or conditional blocks."""
        entries: List[Dict[str, Optional[str]]] = []
        for assignment in block.get_all("icon"):
            if isinstance(assignment.value, Block):
                icon_block = assignment.value
                trigger = icon_block.block("trigger")
                entries.append(
                    {
                        "value": _scalar_value(icon_block, "value"),
                        "trigger": trigger.inner_text.strip() if trigger else None,
                        "raw": assignment.text,
                    }
                )
            else:
                entries.append(
                    {
                        "value": assignment.value.value,
                        "trigger": None,
                        "raw": assignment.text,
                    }
                )

        if not entries:
            return None
        if len(entries) == 1 and entries[0]["trigger"] is None:
            return entries[0]["value"]
        return entries

    def _map_search_filters(self, block: Block) -> List[str]:
        filters: List[str] = []
        for filter_block in block.blocks("search_filters"):
            filters.extend(filter_block.value_strings())
        return _dedup([f.strip() for f in filters])

    def _map_value_list(self, block: Block, key: str) -> Optional[List[str]]:
        """Map a key that may be a bare value or a block of bare values."""
        assignments = block.get_all(key)
        if not assignments:
            return None
        values: List[str] = []
        for assignment in assignments:
            if isinstance(assignment.value, Block):
                values.extend(assignment.value.value_strings())
            else:
                values.append(assignment.value.value)
        return _dedup([v.strip() for v in values])

    def _map_mutually_exclusive(self, block: Block) -> List[str]:
        exclusive: List[str] = []
        for mutex_block in block.blocks("mutually_exclusive"):
            exclusive.extend(_focus_ids(mutex_block))
        return _dedup(exclusive)

    def _map_prerequisites(
        self, block: Block
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Return ``(flat_ids, groups)`` for every prerequisite block.

        Each ``prerequisite = { ... }`` is one group: several ``focus = X``
        entries inside one block mean "any of these" (OR). Explicit ``OR``/
        ``AND`` sub-blocks keep their own kind. The plural ``prerequisites``
        spelling is treated as AND, matching how mods use it.
        """
        flat: List[str] = []
        groups: List[Dict[str, Any]] = []

        for assignment in block.assignments():
            key = assignment.key.value.lower()
            if key not in ("prerequisite", "prerequisites"):
                continue
            if not isinstance(assignment.value, Block):
                continue

            prereq_block = assignment.value
            treat_as_and = key == "prerequisites"
            consumed_ids: List[str] = []

            for logical in ("OR", "AND"):
                for sub_block in prereq_block.blocks(logical):
                    items = _focus_ids(sub_block)
                    if not items:
                        continue
                    consumed_ids.extend(items)
                    groups.append({"type": logical, "items": items})

            direct = [
                item
                for item in _focus_ids(prereq_block, recursive=False)
                if item not in consumed_ids
            ]
            if direct:
                if treat_as_and:
                    kind = "AND"
                else:
                    kind = "AND" if len(direct) == 1 else "OR"
                groups.append({"type": kind, "items": direct})

        for group in groups:
            for item in group["items"]:
                if item not in flat:
                    flat.append(item)

        return flat, groups

    def _map_visibility(self, block: Block) -> Dict[str, Any]:
        """Decide whether a focus is conditionally hidden, and why.

        A ``visible`` block only hides the focus when it actually constrains
        something: an empty block, or one that is just ``always = yes``, leaves
        the focus visible.
        """
        visible_blocks = block.blocks("visible")
        if not visible_blocks:
            return {"hidden": False, "hidden_tags": []}

        hidden = False
        tags: List[str] = []

        for visible in visible_blocks:
            if not visible.children:
                continue
            always = visible.scalar("always")
            if len(visible.children) == 1 and always is not None and always.as_bool():
                continue

            hidden = True
            for condition in _conditions_sorted(visible, "visible"):
                if condition["type"] == "has_completed_focus":
                    tags.append(f"revealed_by_focus:{condition['value']}")
                elif condition["type"] == "has_government":
                    tags.append(f"government:{condition['value']}")
                elif condition["type"] == "has_dlc":
                    tags.append(f"dlc:{condition['value']}")

        if hidden and not tags:
            tags = ["conditional"]

        return {"hidden": hidden, "hidden_tags": _dedup(tags)}

    def _map_conditions(self, block: Block) -> List[Dict[str, str]]:
        """Conditions the GUI's game-state simulation understands."""
        conditions: List[Dict[str, str]] = []
        for key in ("available", "bypass"):
            for sub_block in block.blocks(key):
                conditions.extend(_conditions_sorted(sub_block, key))
        # Legacy behaviour: also surface completion prerequisites found anywhere
        # in the focus, tagged so callers can tell them apart.
        for assignment in block.find_all("has_completed_focus"):
            if isinstance(assignment.value, Scalar):
                value = assignment.value.value.strip()
                if value:
                    conditions.append(
                        {"where": "raw", "type": "has_completed_focus", "value": value}
                    )
        return conditions

    def _split_unknown_blocks(
        self, assignment: Assignment, block: Block
    ) -> Tuple[List[str], str]:
        """Record direct child blocks we do not model, and a copy without them.

        Only *direct* children count: a nested ``OR`` inside ``available`` is
        part of a construct we understand and must not be flagged.
        """
        raw = assignment.text
        base = assignment.start
        unparsed: List[str] = []
        spans: List[Tuple[int, int]] = []

        for child in block.assignments():
            if not isinstance(child.value, Block):
                continue
            if child.key.value.lower() in KNOWN_FOCUS_BLOCK_KEYS:
                continue
            spans.append((child.start - base, child.end - base))
            unparsed.append(child.text)

        if not spans:
            return unparsed, raw

        parts: List[str] = []
        cursor = 0
        for start, end in spans:
            if start > cursor:
                parts.append(raw[cursor:start])
            cursor = max(cursor, end)
        if cursor < len(raw):
            parts.append(raw[cursor:])
        return unparsed, "".join(parts)

    # -------------------------
    # Cross-focus resolution
    # -------------------------
    def _resolve_relative_positions(self, focuses: List[Dict[str, Any]]) -> None:
        """Turn ``relative_position_id`` offsets into absolute coordinates.

        Chains resolve transitively (A relative to B relative to C). A cycle or
        a dangling reference is reported and the focus keeps its raw offset.
        """
        by_id = {f["id"]: f for f in focuses if f.get("id")}
        resolved: Dict[str, Tuple[float, float]] = {}
        in_progress: set = set()

        def absolute(focus: Dict[str, Any]) -> Tuple[float, float]:
            focus_id = focus.get("id")
            if focus_id in resolved:
                return resolved[focus_id]

            offset = (focus.get("x", 0), focus.get("y", 0))
            parent_id = focus.get("relative_position_id")
            if not parent_id:
                resolved[focus_id] = offset
                return offset

            parent = by_id.get(parent_id)
            if parent is None:
                self._report_text(
                    code="unknown-relative-position",
                    message=(
                        f"focus {focus_id!r} is positioned relative to "
                        f"{parent_id!r}, which is not defined in this file"
                    ),
                    expected="a focus id defined in this file",
                    token=str(parent_id),
                    severity="warning",
                )
                resolved[focus_id] = offset
                return offset

            if focus_id in in_progress:
                self._report_text(
                    code="circular-relative-position",
                    message=(
                        f"focus {focus_id!r} is positioned relative to itself "
                        "through a cycle"
                    ),
                    expected="an acyclic relative_position_id chain",
                    token=str(focus_id),
                    severity="warning",
                )
                resolved[focus_id] = offset
                return offset

            in_progress.add(focus_id)
            parent_x, parent_y = absolute(parent)
            in_progress.discard(focus_id)

            position = (parent_x + offset[0], parent_y + offset[1])
            resolved[focus_id] = position
            return position

        for focus in focuses:
            if not focus.get("id"):
                continue
            parent_id = focus.get("relative_position_id")
            offset_x, offset_y = focus.get("x", 0), focus.get("y", 0)
            x, y = absolute(focus)
            focus["x"], focus["y"] = x, y
            if parent_id and parent_id in by_id:
                focus["_relative_to"] = parent_id
                focus["_relative_offset_x"] = offset_x
                focus["_relative_offset_y"] = offset_y

    # -------------------------
    # Diagnostics
    # -------------------------
    def _report(
        self,
        node,
        code: str,
        message: str,
        expected: str,
        severity: str = "error",
    ) -> None:
        line, column = self.source.position(node.start)
        diagnostic = Diagnostic(
            severity=severity,
            code=code,
            message=message,
            filename=self.filename,
            line=line,
            column=column,
            offset=node.start,
            token=self.source.slice(node.start, min(node.end, node.start + 40)),
            expected=expected,
            context=self.source.context(node.start),
        )
        self.diagnostics.append(diagnostic)
        if severity == "error" and not self.recover:
            raise ParadoxSyntaxError([diagnostic])

    def _report_text(
        self,
        code: str,
        message: str,
        expected: str,
        token: str,
        severity: str = "warning",
    ) -> None:
        """Report a whole-document semantic problem with no single source span."""
        self.diagnostics.append(
            Diagnostic(
                severity=severity,
                code=code,
                message=message,
                filename=self.filename,
                line=0,
                column=0,
                offset=0,
                token=token,
                expected=expected,
                context="",
            )
        )


# =============================================================================
# Converter
# =============================================================================

def convert_txt_to_project_dict(
    txt_content: str,
    filename: str = "<string>",
    recover: bool = True,
) -> Dict[str, Any]:
    """Parse focus script and return the project dict the editor loads.

    The result is JSON-serializable throughout: ``diagnostics`` holds plain
    dicts (see :meth:`_paradox_lexer.Diagnostic.to_dict`), not exceptions or
    syntax nodes.
    """
    parser = TxtFocusParser(txt_content, filename=filename, recover=recover)
    result = parser.parse()

    for focus in result.get("focuses") or []:
        focus.setdefault("id", "")
        focus.setdefault("name", focus.get("id") or "")
        focus.setdefault("cost", 10)
        focus.setdefault("x", 0)
        focus.setdefault("y", 0)

    return result


# -------------------------
# CLI
# -------------------------
def main():
    import argparse
    import sys

    ap = argparse.ArgumentParser(description='Parse HOI4 focus tree files into project JSON')
    ap.add_argument('input_file', help='Path to HOI4 focus .txt file')
    ap.add_argument('-o', '--output', help='Output JSON file (default stdout)')
    ap.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    ap.add_argument(
        '--strict',
        action='store_true',
        help='Fail on the first syntax error instead of recovering',
    )

    args = ap.parse_args()

    try:
        with open(args.input_file, 'r', encoding='utf-8-sig') as f:
            txt_content = f.read()
    except FileNotFoundError:
        print(f"Error: File '{args.input_file}' not found", file=sys.stderr)
        raise SystemExit(1)
    except OSError as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        raise SystemExit(1)

    try:
        result = convert_txt_to_project_dict(
            txt_content, filename=args.input_file, recover=not args.strict
        )
    except ParadoxSyntaxError as e:
        for diagnostic in e.diagnostics:
            print(diagnostic.format(), file=sys.stderr)
        raise SystemExit(2)

    for record in result.get('diagnostics', []):
        print(
            f"{record['filename']}:{record['line']}:{record['column']}: "
            f"{record['severity']} [{record['code']}]: {record['message']}",
            file=sys.stderr,
        )

    json_kwargs = {'indent': 2} if args.pretty else {}
    out = json.dumps(result, **json_kwargs)
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(out)
        print(f"Wrote output to {args.output}")
    else:
        print(out)


if __name__ == '__main__':
    main()
