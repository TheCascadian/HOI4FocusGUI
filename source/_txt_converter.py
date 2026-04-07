# region File (auto-generated)
# endregion

# _txt_converter.py
# Fixed HOI4 focus .txt parser with proper positioning, connections, and visibility
from _imports import (
    # Standard library
    json, re, sys,
    # Typing
    Any, Dict, List, Optional, Tuple, Union,
)



class TxtFocusParser:
    """Parse HOI4 focus .txt content into a structured dict with focuses and tree info."""

    def __init__(self, content: str):
        self.content = content
        self.lines = content.splitlines()
        self.idx = 0
        self.len = len(self.lines)

    # -------------------------
    # High-level parse entry
    # -------------------------
    def parse(self) -> Dict[str, Any]:
        """Parse the entire content."""
        result = {
            'focuses': [],
            'tree_info': {},
            'raw': self.content
        }

        blocks = []

        # Find all focus_tree blocks and focus blocks
        for start, end, inner in self._find_named_blocks_with_spans(self.content, 'focus_tree'):
            full = self.content[start:end]
            blocks.append((start, 'focus_tree', inner, full))

        for start, end, inner in self._find_named_blocks_with_spans(self.content, 'focus'):
            full = self.content[start:end]
            blocks.append((start, 'focus', inner, full))

        blocks.sort(key=lambda t: t[0])

        for _, kind, inner, full in blocks:
            try:
                if kind == 'focus_tree':
                    result['tree_info'] = self._parse_tree_info(full)
                elif kind == 'focus':
                    fdict = self._parse_focus_block(full)
                    if fdict:
                        result['focuses'].append(fdict)
            except Exception as e:
                print(f"Warning: Failed to parse {kind} block: {e}")
                continue

        # CRITICAL FIX: Calculate absolute positions after all focuses are parsed
        self._resolve_relative_positions(result['focuses'])

        return result

    # -------------------------
    # NEW: Resolve relative positions
    # -------------------------
    def _resolve_relative_positions(self, focuses: List[Dict[str, Any]]):
        """Calculate absolute positions for focuses with relative_position_id."""
        # Create lookup dict
        focus_by_id = {f['id']: f for f in focuses if f.get('id')}

        # Process focuses that have relative positioning
        for focus in focuses:
            rel_id = focus.get('relative_position_id')
            if rel_id:
                parent = focus_by_id.get(rel_id)
                if parent:
                    # Add parent's position to this focus's offset
                    focus['x'] = parent.get('x', 0) + focus.get('x', 0)
                    focus['y'] = parent.get('y', 0) + focus.get('y', 0)
                    # Store original relative info for reference
                    focus['_relative_to'] = rel_id
                    focus['_relative_offset_x'] = focus.get('x', 0) - parent.get('x', 0)
                    focus['_relative_offset_y'] = focus.get('y', 0) - parent.get('y', 0)

    # -------------------------
    # Tree-level parsing
    # -------------------------
    def _parse_tree_info(self, raw_block: str) -> Dict[str, Any]:
        tree_info = {
            'id': None,
            'country': [],
            'reset_on_civil_war': None,
            'shared_focus': None,
            'continuous_focus_position': None,
            'default': None,
            'initial_show_position': None,
            'raw': raw_block
        }

        # Tree id
        m = re.search(r'^\s*id\s*=\s*(.+)$', raw_block, re.MULTILINE | re.IGNORECASE)
        if m:
            tree_info['id'] = m.group(1).strip().strip('"')

        # Boolean properties
        for k in ('default', 'shared_focus', 'reset_on_civil_war'):
            m = re.search(rf'{k}\s*=\s*([^\n#]+)', raw_block, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if k == 'shared_focus':
                    tree_info['shared_focus'] = val.strip().strip('"')
                elif k == 'default':
                    tree_info['default'] = val.lower() in ('yes', 'true', '1')
                elif k == 'reset_on_civil_war':
                    tree_info['reset_on_civil_war'] = val.lower() in ('yes', 'true', '1')

        # Extract country tags
        country_blocks = self._find_named_blocks(raw_block, 'country')
        for cb in country_blocks:
            tag_matches = re.finditer(r'tag\s*=\s*([A-Z]{2,3})', cb, re.IGNORECASE)
            for tm in tag_matches:
                tree_info['country'].append(tm.group(1).upper())

        # Coordinates blocks
        tree_info['continuous_focus_position'] = self._extract_coordinate_block(raw_block, 'continuous_focus_position')
        tree_info['initial_show_position'] = self._extract_coordinate_block(raw_block, 'initial_show_position')

        return tree_info

    def _extract_coordinate_block(self, content: str, block_name: str) -> Optional[Dict[str, int]]:
        blocks = self._find_named_blocks(content, block_name)
        if not blocks:
            return None
        block_content = blocks[0]
        x_match = re.search(r'x\s*=\s*(-?\d+)', block_content)
        y_match = re.search(r'y\s*=\s*(-?\d+)', block_content)
        coords = {}
        if x_match:
            coords['x'] = int(x_match.group(1))
        if y_match:
            coords['y'] = int(y_match.group(1))
        return coords if coords else None

    # -------------------------
    # Focus-level parsing
    # -------------------------
    def _parse_focus_block(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parse a single focus block."""
        f = {
            'id': None,
            'name': None,
            'cost': None,
            'icon': None,
            'x': 0,
            'y': 0,
            'relative_position_id': None,
            'prerequisites': [],
            'mutually_exclusive': [],
            'ai_will_do': None,
            'completion_reward': None,
            'select_effect': None,
            'remove_effect': None,
            'description': None,
            'available': None,
            'bypass': None,
            'cancel': None,
            'cancel_if_invalid': None,
            'continue_if_invalid': None,
            'available_if_capitulated': None,
            'will_lead_to_war_with': None,
            'search_filters': [],
            'allow_branch': None,
            'raw': raw,
        }

        # Parse complex structures first
        prereq_flat, prereq_groups = self._parse_prerequisites(raw)
        f['prerequisites'] = prereq_flat
        f['prerequisites_groups'] = prereq_groups
        f['mutually_exclusive'] = self._parse_mutually_exclusive(raw)
        f['search_filters'] = self._parse_search_filters(raw)
        f['icon'] = self._parse_icon(raw)

        # Simple line-based parsing
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s.startswith('#'):
                continue

            # ID
            m = re.match(r'^\s*id\s*=\s*(.+)$', s, re.IGNORECASE)
            if m:
                f['id'] = m.group(1).strip().strip('"')
                continue

            # Name/Text
            m = re.match(r'^\s*(?:text|name)\s*=\s*(.+)$', s, re.IGNORECASE)
            if m:
                f['name'] = m.group(1).strip().strip('"')
                continue

            # X coordinate
            m = re.match(r'^\s*x\s*=\s*(-?\d+)', s, re.IGNORECASE)
            if m:
                f['x'] = int(m.group(1))
                continue

            # Y coordinate
            m = re.match(r'^\s*y\s*=\s*(-?\d+)', s, re.IGNORECASE)
            if m:
                f['y'] = int(m.group(1))
                continue

            # Relative position
            m = re.match(r'^\s*relative_position_id\s*=\s*(.+)$', s, re.IGNORECASE)
            if m:
                f['relative_position_id'] = m.group(1).strip().strip('"')
                continue

            # Cost
            m = re.match(r'^\s*cost\s*=\s*(.+)$', s, re.IGNORECASE)
            if m:
                try:
                    f['cost'] = int(re.sub(r"[^0-9-]", "", m.group(1)))
                except:
                    f['cost'] = 10
                continue

            # Boolean flags
            for flag in ('cancel_if_invalid', 'continue_if_invalid', 'available_if_capitulated'):
                m = re.match(rf'^\s*{flag}\s*=\s*(.+)$', s, re.IGNORECASE)
                if m:
                    f[flag] = m.group(1).strip().lower() in ('yes', 'true', '1')
                    break

        # Parse nested blocks
        for key in ('ai_will_do', 'completion_reward', 'select_effect', 'remove_effect',
                    'available', 'bypass', 'cancel', 'allow_branch', 'visible'):
            blocks = self._find_named_blocks(raw, key)
            if blocks:
                f[key] = blocks[0].strip() if len(blocks) == 1 else [b.strip() for b in blocks]

        # Defaults
        if not f['id']:
            m = re.search(r'id\s*=\s*([0-9A-Za-z_\.:-]+)', raw, re.IGNORECASE)
            if m:
                f['id'] = m.group(1).strip().strip('"')

        if not f['id'] and not f['name']:
            return None

        f.setdefault('cost', 10)
        f.setdefault('name', f['id'] or '')

        # -------------------------
        # Detect unrecognized named brace-blocks so caller can
        # optionally cull/skip them during import while keeping
        # the rest of the focus intact.
        known = set([
            'ai_will_do', 'completion_reward', 'select_effect', 'remove_effect',
            'available', 'bypass', 'cancel', 'allow_branch', 'visible', 'icon',
            'prerequisite', 'prerequisites', 'mutually_exclusive', 'search_filters',
            'trigger', 'options', 'option', 'completion_reward'
        ])

        unparsed_blocks: List[str] = []
        spans_to_remove: List[Tuple[int, int]] = []

        for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{', raw):
            name = m.group(1)
            start = m.start()
            end_pos, inner = self._extract_braced_from(raw, m.end() - 1)
            if end_pos is None:
                continue
            if name.lower() not in known:
                spans_to_remove.append((start, end_pos + 1))
                unparsed_blocks.append(raw[start:end_pos + 1])

        # Build a cleaned version of the raw focus with unrecognized blocks removed
        clean_raw = raw
        if spans_to_remove:
            # Merge and invert spans to produce cleaned text
            spans_to_remove.sort()
            out_parts: List[str] = []
            last = 0
            for s, e in spans_to_remove:
                if s > last:
                    out_parts.append(raw[last:s])
                last = e
            if last < len(raw):
                out_parts.append(raw[last:])
            clean_raw = ''.join(out_parts)

        f['raw_unparsed'] = unparsed_blocks
        f['has_unparsed'] = bool(unparsed_blocks)
        f['clean_raw'] = clean_raw

        return f

    # -------------------------
    # FIXED: Icon parsing
    # -------------------------
    def _parse_icon(self, raw_content: str) -> Optional[Union[str, List[Dict[str, Optional[str]]]]]:
        """Parse icon - handles both simple (icon = GFX_foo) and conditional blocks."""
        results: List[Dict[str, Optional[str]]] = []
        covered_spans = []

        # Find brace-style icon blocks
        icon_blocks = self._find_named_blocks_with_spans(raw_content, 'icon')
        for span_start, span_end, block_text in icon_blocks:
            covered_spans.append((span_start, span_end))
            full_block_raw = raw_content[span_start:span_end]

            # Extract value
            value_match = re.search(r'value\s*=\s*([^\n#]+)', block_text)
            value = value_match.group(1).strip().strip('"') if value_match else None

            # Extract trigger
            trigger_blocks = self._find_named_blocks(block_text, 'trigger')
            trigger_text = trigger_blocks[0].strip() if trigger_blocks else None

            results.append({'value': value, 'trigger': trigger_text, 'raw': full_block_raw})

        # Find simple icon = VALUE declarations
        for m in re.finditer(r'icon\s*=\s*(?!\{)\s*([^\n#]+)', raw_content, re.IGNORECASE):
            s, e = m.span()
            # Skip if inside a brace block
            inside = any(s >= a and e <= b for a, b in covered_spans)
            if not inside:
                val = m.group(1).strip().strip('"')
                results.append({'value': val, 'trigger': None, 'raw': m.group(0)})

        if not results:
            return None
        # Return simple string if only one unconditional icon
        if len(results) == 1 and results[0].get('trigger') is None:
            return results[0]['value']
        return results

    # -------------------------
    # Search filters
    # -------------------------
    def _parse_search_filters(self, raw_content: str) -> List[str]:
        filters: List[str] = []
        match = re.search(r'search_filters\s*=\s*\{([^}]*)\}', raw_content, re.IGNORECASE | re.DOTALL)
        if match:
            filter_content = match.group(1)
            filter_matches = re.findall(r'([A-Z_][A-Z0-9_]*)', filter_content)
            filters.extend(filter_matches)
        return filters

    # -------------------------
    # FIXED: Prerequisites parsing
    # -------------------------
    def _parse_prerequisites(self, raw_content: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Parse prerequisites, returning (flat_list, grouped_defs)."""
        flat: List[str] = []
        groups: List[Dict[str, Any]] = []

        for match in re.finditer(r'(prerequisites?)\s*=\s*\{', raw_content, re.IGNORECASE):
            keyword = match.group(1).lower()
            end_pos, block_text = self._extract_braced_from(raw_content, match.end() - 1)
            if end_pos is None or not block_text:
                continue

            block_groups = self._parse_prerequisite_block(block_text, keyword == 'prerequisites')
            for grp in block_groups:
                items = [it for it in grp.get('items', []) if it]
                if not items:
                    continue
                grp_type = str(grp.get('type') or 'OR').upper()
                normalized = {'type': grp_type, 'items': []}
                for item in items:
                    if item not in flat:
                        flat.append(item)
                    if item not in normalized['items']:
                        normalized['items'].append(item)
                groups.append(normalized)

        return flat, groups

    def _parse_prerequisite_block(self, block_text: str, treat_as_and: bool) -> List[Dict[str, Any]]:
        groups: List[Dict[str, Any]] = []
        consumed: List[Tuple[int, int]] = []

        for name in ('OR', 'AND'):
            for start, end, inner in self._find_named_blocks_with_spans(block_text, name):
                items = self._extract_focus_ids(inner)
                if not items:
                    continue
                groups.append({'type': name, 'items': items})
                consumed.append((start, end))

        consumed.sort()
        remainder_parts: List[str] = []
        last = 0
        for start, end in consumed:
            if last < start:
                remainder_parts.append(block_text[last:start])
            last = max(last, end)
        if last < len(block_text):
            remainder_parts.append(block_text[last:])
        remainder_text = ''.join(remainder_parts)

        direct_items = self._extract_focus_ids(remainder_text)
        if direct_items:
            if treat_as_and:
                groups.append({'type': 'AND', 'items': direct_items})
            else:
                g_type = 'AND' if len(direct_items) == 1 else 'OR'
                groups.append({'type': g_type, 'items': direct_items})

        return groups

    def _extract_focus_ids(self, text: str) -> List[str]:
        ids: List[str] = []
        for focus_match in re.finditer(r'focus\s*=\s*([0-9A-Za-z_\.:-]+)', text):
            focus_id = focus_match.group(1).strip().strip('"')
            if focus_id and focus_id not in ids:
                ids.append(focus_id)
        return ids

    # -------------------------
    # Mutually exclusive
    # -------------------------
    def _parse_mutually_exclusive(self, raw_content: str) -> List[str]:
        exclusive_focuses: List[str] = []
        for match in re.finditer(r'mutually_exclusive\s*=\s*\{', raw_content, re.IGNORECASE):
            end_pos, block_text = self._extract_braced_from(raw_content, match.end() - 1)
            if end_pos is None:
                continue
            for focus_match in re.finditer(r'focus\s*=\s*([0-9A-Za-z_\.:-]+)', block_text):
                focus_id = focus_match.group(1).strip().strip('"')
                if focus_id and focus_id not in exclusive_focuses:
                    exclusive_focuses.append(focus_id)
        return exclusive_focuses

    # -------------------------
    # Generic block finders
    # -------------------------
    def _find_named_blocks(self, content: str, name: str) -> List[str]:
        """Return inner content of each 'name = { ... }' occurrence."""
        results: List[str] = []
        for match in re.finditer(rf'{re.escape(name)}\s*=\s*\{{', content, re.IGNORECASE):
            start_idx = match.end() - 1
            end_pos, block_text = self._extract_braced_from(content, start_idx)
            if end_pos is not None and block_text:
                results.append(block_text)
        return results

    def _find_named_blocks_with_spans(self, content: str, name: str) -> List[Tuple[int, int, str]]:
        """Return (start_index, end_index, inner_text) for each named block."""
        results: List[Tuple[int, int, str]] = []
        for match in re.finditer(rf'{re.escape(name)}\s*=\s*\{{', content, re.IGNORECASE):
            brace_open_pos = match.end() - 1
            end_pos, block_text = self._extract_braced_from(content, brace_open_pos)
            if end_pos is not None and block_text is not None:
                results.append((match.start(), end_pos + 1, block_text))
        return results

    def _extract_braced_from(self, content: str, brace_open_pos: int) -> Tuple[Optional[int], Optional[str]]:
        """Find matching closing brace and return (end_index, inner_text)."""
        idx = brace_open_pos
        while idx < len(content) and content[idx] != '{':
            if not content[idx].isspace():
                return None, None
            idx += 1

        if idx >= len(content):
            return None, None

        i = idx
        brace_count = 0
        in_string = False
        escape_next = False
        inner_start = idx + 1

        while i < len(content):
            ch = content[i]
            if escape_next:
                escape_next = False
                i += 1
                continue
            if ch == '\\':
                escape_next = True
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                i += 1
                continue
            if not in_string:
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        return i, content[inner_start:i]
            i += 1

        return None, None

# -------------------------
# FIXED: Converter with proper visibility detection
# -------------------------
def convert_txt_to_project_dict(txt_content: str) -> Dict[str, Any]:
    parser = TxtFocusParser(txt_content)
    result = parser.parse()

    focuses = result.get('focuses', []) or []

    for fx in focuses:
        # Ensure defaults
        fx.setdefault('id', '')
        fx.setdefault('name', fx.get('id') or '')
        fx.setdefault('cost', 10)
        fx.setdefault('x', 0)
        fx.setdefault('y', 0)

        # FIXED: Hidden/visible detection
        # A focus is only hidden if it has a NON-TRIVIAL visible block
        hidden = False
        hidden_tags: List[str] = []

        vis = fx.get('visible')
        if vis is not None:
            vis_text = vis if isinstance(vis, str) else "\n".join(str(v) for v in vis if v)

            # Check if it's trivially always visible
            is_always_visible = bool(re.search(r'^\s*always\s*=\s*(yes|true|1)\s*$',
                                               vis_text, re.MULTILINE | re.IGNORECASE))

            # Check if it's empty (just whitespace/comments)
            is_empty = not bool(re.search(r'[a-zA-Z0-9_]', vis_text))

            if not is_always_visible and not is_empty:
                hidden = True

                # Extract reveal conditions
                for m in re.finditer(r'has_completed_focus\s*=\s*([A-Za-z0-9_\.:-]+)', vis_text):
                    hidden_tags.append(f'revealed_by_focus:{m.group(1).strip()}')

                for m in re.finditer(r'has_government\s*=\s*([A-Za-z0-9_\.:-]+)', vis_text, re.IGNORECASE):
                    hidden_tags.append(f'government:{m.group(1).strip().lower()}')

                for m in re.finditer(r'has_dlc\s*=\s*"([^"]+)"', vis_text, re.IGNORECASE):
                    hidden_tags.append(f'dlc:{m.group(1).strip()}')

                # Generic conditional tag if no specific tags found
                if not hidden_tags:
                    hidden_tags = ['conditional']

        fx['hidden'] = hidden
        fx['hidden_tags'] = list(dict.fromkeys(hidden_tags))

        # NEW: Extract simple structured conditions from 'available' and 'bypass' blocks
        # so the GUI can simulate game state. We only extract a few common condition types
        # here (has_completed_focus, has_government, has_dlc). Each condition entry is a
        # dict: { 'where': 'available'|'bypass', 'type': 'has_completed_focus'|'has_government'|'has_dlc', 'value': <str> }
        avail_conditions = []
        def extract_conditions_from(text, where):
            if not text:
                return
            txt = text if isinstance(text, str) else "\n".join(text)
            for m in re.finditer(r'has_completed_focus\s*=\s*([A-Za-z0-9_\.:-]+)', txt):
                avail_conditions.append({'where': where, 'type': 'has_completed_focus', 'value': m.group(1).strip()})
            for m in re.finditer(r'has_government\s*=\s*([A-Za-z0-9_\.:-]+)', txt, re.IGNORECASE):
                avail_conditions.append({'where': where, 'type': 'has_government', 'value': m.group(1).strip().lower()})
            for m in re.finditer(r'has_dlc\s*=\s*"([^"]+)"', txt, re.IGNORECASE):
                avail_conditions.append({'where': where, 'type': 'has_dlc', 'value': m.group(1).strip()})

        extract_conditions_from(fx.get('available'), 'available')
        extract_conditions_from(fx.get('bypass'), 'bypass')
        # Also look in the raw block for common reveal conditions (legacy cases)
        raw = fx.get('raw', '') or ''
        for m in re.finditer(r'has_completed_focus\s*=\s*([A-Za-z0-9_\.:-]+)', raw):
            avail_conditions.append({'where': 'raw', 'type': 'has_completed_focus', 'value': m.group(1).strip()})

        fx['avail_conditions'] = avail_conditions

        # FIXED: Prerequisites format for rendering
        # Keep groups for proper line drawing (AND vs OR logic)
        raw_groups = fx.get('prerequisites_groups', []) or []
        normalized_groups: List[Dict[str, Any]] = []
        if isinstance(raw_groups, list):
            for grp in raw_groups:
                if isinstance(grp, dict):
                    items = [item for item in grp.get('items', []) if item]
                    if not items:
                        continue
                    gtype = str(grp.get('type') or ('OR' if len(items) > 1 else 'AND')).upper()
                    normalized_groups.append({'type': gtype, 'items': items})
                elif isinstance(grp, list):
                    items = [item for item in grp if item]
                    if not items:
                        continue
                    gtype = 'OR' if len(items) > 1 else 'AND'
                    normalized_groups.append({'type': gtype, 'items': items})
                elif isinstance(grp, str) and grp:
                    normalized_groups.append({'type': 'AND', 'items': [grp]})
        fx['prerequisites_groups'] = normalized_groups

        flat: List[str] = []
        raw_flat = fx.get('prerequisites', []) or []
        if isinstance(raw_flat, list):
            for item in raw_flat:
                if isinstance(item, str):
                    if item and item not in flat:
                        flat.append(item)
                elif isinstance(item, list):
                    for sub in item:
                        if sub and sub not in flat:
                            flat.append(sub)
        elif isinstance(raw_flat, str):
            if raw_flat:
                flat.append(raw_flat)
        fx['prerequisites'] = flat

    result['focuses'] = focuses
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

    args = ap.parse_args()

    try:
        with open(args.input_file, 'r', encoding='utf-8') as f:
            txt_content = f.read()
    except FileNotFoundError:
        print(f"Error: File '{args.input_file}' not found", file=sys.stderr)
        raise SystemExit(1)
    except Exception as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        raise SystemExit(1)

    try:
        result = convert_txt_to_project_dict(txt_content)
        json_kwargs = {'indent': 2} if args.pretty else {}
        out = json.dumps(result, **json_kwargs)
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(out)
            print(f"Wrote output to {args.output}")
        else:
            print(out)
    except Exception as e:
        print(f"Error parsing: {e}", file=sys.stderr)
        raise SystemExit(2)

if __name__ == '__main__':
    main()