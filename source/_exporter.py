# _exporter.py
"""
HOI4 Focus Tree Exporter
Converts FocusTree dataclass instances back to .txt format
Also exports HOI4 state files from edited state metadata
"""

from _imports import (
    # Standard library
    os, re,
    # Typing
    Any, Dict, List, Optional, Tuple,
    # Project
    Focus,
)

import logging
from _dataStructs import Focus, FocusBranch, FocusTree
from _dataStructs import Focus, FocusBranch, FocusTree


# region Focus Tree Exporter

class HOI4Exporter:
    """Exporter for HOI4 focus trees"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.indent = "\t"
        self.last_export_summary: Optional[Dict[str, Any]] = None

    def export_tree(
        self,
        tree: FocusTree,
        destination: str,
        *,
        project_name: Optional[str] = None,
        focus_filename: Optional[str] = None,
        ensure_unique: bool = False,
        use_mod_structure: bool = True,
        encoding: str = 'utf-8-sig',
        # New optional outputs
        write_localisation: bool = True,
        localisation_language: str = 'english',
        localisation_languages: Optional[List[str]] = None,
        localisation_filename: Optional[str] = None,
        write_gfx: bool = True,
        gfx_filename: Optional[str] = None,
        icon_source_dirs: Optional[List[str]] = None,
    ) -> bool:
        """Export ``tree`` to disk.

        Args:
            tree: Focus tree payload to export.
            destination: When ``project_name`` is provided, treated as the base directory
                that will contain per-project subfolders. Otherwise interpreted as either
                a directory (export file written inside) or a concrete file path.
            project_name: Optional mod/project identifier. When set, the exporter creates
                ``<destination>/<project_slug>/common/national_focus`` (or the project root
                directly when ``use_mod_structure`` is False) before writing the focus file.
            focus_filename: Optional filename override for the focus script. ``.txt`` is
                appended automatically when missing.
            ensure_unique: When True, append a numeric suffix if the target file already
                exists. The summary exposes whether a rename occurred.
            use_mod_structure: When True (default) and ``project_name`` is provided, create
                the HOI4 mod directory layout ``common/national_focus`` inside the project
                folder.
            encoding: File encoding used for the exported focus script.

        Returns:
            ``True`` on success, ``False`` otherwise. Additional details about the export are
            stored on ``self.last_export_summary``.
        """

        self.last_export_summary = None
        try:
            focus_path, summary = self._resolve_focus_export_path(
                tree=tree,
                destination=destination,
                project_name=project_name,
                focus_filename=focus_filename,
                ensure_unique=ensure_unique,
                use_mod_structure=use_mod_structure,
            )
            content = self.tree_to_string(tree)
            with open(focus_path, 'w', encoding=encoding) as f:
                f.write(content)
            summary['encoding'] = encoding

            # Optional: write localisation and gfx/icon assets when we have a project root
            project_root = summary.get('project_root')
            project_slug = summary.get('project_slug') or self._slugify(getattr(tree, 'tree_name', None) or getattr(tree, 'id', None) or 'focus_project', 'focus_project')
            icon_source_dirs = icon_source_dirs or []

            if project_root and write_localisation:
                langs: List[str] = list(localisation_languages or [localisation_language])
                loc_paths: Dict[str, str] = {}
                for lang in langs:
                    loc_path = self._write_localisation(
                        tree,
                        project_root,
                        language=lang,
                        filename_override=localisation_filename,
                    )
                    loc_paths[lang] = loc_path

                # Back-compat: if single language, expose 'localisation_path'; always include map
                if len(loc_paths) == 1:
                    summary['localisation_path'] = next(iter(loc_paths.values()))
                summary['localisation_paths'] = loc_paths
                summary['localisation_languages'] = list(loc_paths.keys())

            if project_root and write_gfx:
                gfx_info = self._write_gfx_and_copy_icons(
                    tree,
                    project_root,
                    project_slug=project_slug,
                    filename_override=gfx_filename,
                    icon_source_dirs=icon_source_dirs,
                )
                summary.update(gfx_info)

            self.last_export_summary = summary
            return True
        except Exception as e:
            self.logger.error(f"Failed to export tree: {e}")
            self.last_export_summary = None
            return False

    def get_last_export_summary(self) -> Optional[Dict[str, Any]]:
        """Return metadata from the most recent export operation."""
        return self.last_export_summary

    def _resolve_focus_export_path(
        self,
        tree: FocusTree,
        destination: str,
        project_name: Optional[str],
        focus_filename: Optional[str],
        ensure_unique: bool,
        use_mod_structure: bool,
    ) -> Tuple[str, Dict[str, Any]]:
        """Determine the concrete file path for the focus export."""

        if not destination:
            raise ValueError("destination path must be provided")

        filename = self._normalize_focus_filename(focus_filename, tree)
        fallback_slug = tree.tree_name or tree.id or "focus_project"
        project_slug: Optional[str] = None

        if project_name:
            base_dir = destination or os.getcwd()
            if os.path.splitext(base_dir)[1] and not os.path.isdir(base_dir):
                base_dir = os.path.dirname(base_dir)
            if not base_dir:
                base_dir = os.getcwd()
            base_dir = os.path.abspath(base_dir)
            project_slug = self._slugify(project_name, fallback_slug)
            project_root = os.path.join(base_dir, project_slug)
            project_root = os.path.abspath(project_root)
            if use_mod_structure:
                focus_dir = os.path.join(project_root, 'common', 'national_focus')
            else:
                focus_dir = project_root
            focus_dir = os.path.abspath(focus_dir)
            os.makedirs(focus_dir, exist_ok=True)
            requested_path = os.path.join(focus_dir, filename)
        else:
            destination = destination or os.getcwd()
            abs_dest = os.path.abspath(destination)
            if os.path.isdir(abs_dest) or (not os.path.exists(abs_dest) and not os.path.splitext(abs_dest)[1]):
                focus_dir = abs_dest
                os.makedirs(focus_dir, exist_ok=True)
                requested_path = os.path.join(focus_dir, filename)
            else:
                focus_dir = os.path.dirname(abs_dest) or os.getcwd()
                focus_dir = os.path.abspath(focus_dir)
                os.makedirs(focus_dir, exist_ok=True)
                requested_path = abs_dest
            project_root = focus_dir

        requested_path = os.path.abspath(requested_path)
        project_root = os.path.abspath(project_root)
        focus_dir = os.path.abspath(os.path.dirname(requested_path))

        target_path = self._ensure_unique_path(requested_path) if ensure_unique else requested_path
        target_path = os.path.abspath(target_path)

        summary: Dict[str, Any] = {
            'focus_path': target_path,
            'requested_focus_path': requested_path,
            'renamed_due_to_collision': os.path.normcase(target_path) != os.path.normcase(requested_path),
            'project_root': project_root,
            'focus_dir': focus_dir,
            'project_slug': project_slug,
            'project_name': project_name,
        }

        return target_path, summary

    def _normalize_focus_filename(self, requested: Optional[str], tree: FocusTree) -> str:
        """Return a filesystem-safe filename (with extension) for the focus script."""

        base_default = tree.tree_name or tree.id or 'focus_tree'
        if requested:
            stem, ext = os.path.splitext(requested)
            stem = stem or base_default
            ext = ext or '.txt'
        else:
            stem = base_default
            ext = '.txt'
        stem = self._slugify(stem, base_default)
        if not ext.startswith('.'):
            ext = f'.{ext}'
        return f"{stem}{ext}"

    def _slugify(self, value: str, fallback: str) -> str:
        """Sanitize ``value`` for safe directory/file names."""

        candidate = str(value or '').strip()
        if not candidate:
            candidate = str(fallback or '').strip()
        candidate = re.sub(r'[^0-9A-Za-z_\-]+', '_', candidate)
        candidate = candidate.strip('_')
        return candidate or 'focus_project'

    def _ensure_unique_path(self, path: str) -> str:
        """Append a numeric suffix if ``path`` already exists."""

        if not os.path.exists(path):
            return path

        base, ext = os.path.splitext(path)
        ext = ext or '.txt'
        counter = 1
        while True:
            candidate = f"{base}_{counter}{ext}"
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def tree_to_string(self, tree: FocusTree) -> str:
        """Convert FocusTree to HOI4 script string"""
        lines = []

        # Export search filter priorities if present
        if tree.search_filter_priorities:
            lines.append("search_filter_prios = {")
            for key, value in tree.search_filter_priorities.items():
                lines.append(f"{self.indent}{key} = {value}")
            lines.append("}")
            lines.append("")

        # Start focus_tree block
        lines.append("focus_tree = {")
        lines.append(f"{self.indent}id = {tree.id}")
        lines.append("")

        # Country block
        lines.append(f"{self.indent}country = {{")
        lines.append(f"{self.indent}{self.indent}factor = {tree.country_factor}")
        lines.append(f"{self.indent}}}")
        lines.append("")

        # Tree properties
        if tree.is_default:
            lines.append(f"{self.indent}default = yes")
        lines.append(f"{self.indent}reset_on_civilwar = {'yes' if tree.reset_on_civilwar else 'no'}")
        lines.append("")

        # Initial show position
        if tree.initial_show_position:
            lines.append(f"{self.indent}initial_show_position = {{")
            lines.append(f"{self.indent}{self.indent}focus = {tree.initial_show_position}")
            lines.append(f"{self.indent}}}")
            lines.append("")

        # Shared focuses
        for shared in tree.shared_focuses:
            lines.append(f"{self.indent}shared_focus = {shared}")
        if tree.shared_focuses:
            lines.append("")

        # Export all focuses
        for focus in tree.focuses:
            focus_lines = self._export_focus(focus, depth=1)
            lines.extend(focus_lines)
            lines.append("")

        lines.append("}")

        return '\n'.join(lines)

    def _export_focus(self, focus: Focus, depth: int = 0) -> List[str]:
        """Export a single focus to script lines"""
        lines = []
        ind = self.indent * depth

        lines.append(f"{ind}focus = {{")

        # Basic properties
        lines.append(f"{ind}{self.indent}id = {focus.id}")
        if focus.icon:
            lines.append(f"{ind}{self.indent}icon = {focus.icon}")

        # Position
        relative_pos = getattr(focus, 'relative_position_id', None)
        if relative_pos:
            lines.append(f"{ind}{self.indent}x = {focus.x}")
            lines.append(f"{ind}{self.indent}y = {focus.y}")
            lines.append(f"{ind}{self.indent}relative_position_id = {relative_pos}")
        else:
            lines.append(f"{ind}{self.indent}x = {focus.x}")
            lines.append(f"{ind}{self.indent}y = {focus.y}")

        lines.append(f"{ind}{self.indent}cost = {focus.cost}")

        # Search filters
        search_filters = getattr(focus, 'search_filters', None)
        if search_filters:
            lines.append(f"{ind}{self.indent}search_filters = {{ {' '.join(search_filters)} }}")

        # Prerequisites (support AND/OR grouped blocks)
        if getattr(focus, 'prerequisites_groups', None):
            for group in focus.prerequisites_groups:
                gtype = str(group.get('type') or '').upper().strip()
                items = list(group.get('items') or [])
                if gtype in ('AND', 'OR'):
                    lines.append(f"{ind}{self.indent}prerequisite = {{ {gtype} = {{")
                    for item in items:
                        lines.append(f"{ind}{self.indent}{self.indent}focus = {item}")
                    lines.append(f"{ind}{self.indent}}}}}")
                else:
                    # Fallback: simple grouped prerequisite without explicit AND/OR
                    lines.append(f"{ind}{self.indent}prerequisite = {{")
                    for item in items:
                        lines.append(f"{ind}{self.indent}{self.indent}focus = {item}")
                    lines.append(f"{ind}{self.indent}}}")
        elif focus.prerequisites:
            if focus.prerequisites_grouped:
                lines.append(f"{ind}{self.indent}prerequisite = {{")
                for prereq in focus.prerequisites:
                    lines.append(f"{ind}{self.indent}{self.indent}focus = {prereq}")
                lines.append(f"{ind}{self.indent}}}")
            else:
                for prereq in focus.prerequisites:
                    lines.append(f"{ind}{self.indent}prerequisite = {{ focus = {prereq} }}")

        # Mutually exclusive
        for mutex in focus.mutually_exclusive:
            lines.append(f"{ind}{self.indent}mutually_exclusive = {{ focus = {mutex} }}")

        # Branch visibility condition
        if getattr(focus, 'allow_branch', None):
            lines.append(f"{ind}{self.indent}allow_branch = {{")
            for line in str(focus.allow_branch).split('\n'):
                if line.strip():
                    lines.append(f"{ind}{self.indent}{self.indent}{line}")
            lines.append(f"{ind}{self.indent}}}")

        # Availability
        if focus.available:
            lines.append(f"{ind}{self.indent}available = {{")
            for line in focus.available.split('\n'):
                lines.append(f"{ind}{self.indent}{self.indent}{line}")
            lines.append(f"{ind}{self.indent}}}")

        available_if_cap = getattr(focus, 'available_if_capitulated', False)
        if available_if_cap:
            lines.append(f"{ind}{self.indent}available_if_capitulated = yes")

        # Bypass
        if focus.bypass:
            lines.append(f"{ind}{self.indent}bypass = {{")
            for line in focus.bypass.split('\n'):
                lines.append(f"{ind}{self.indent}{self.indent}{line}")
            lines.append(f"{ind}{self.indent}}}")

        # Flags
        cancel_if_invalid = getattr(focus, 'cancel_if_invalid', False)
        if cancel_if_invalid:
            lines.append(f"{ind}{self.indent}cancel_if_invalid = yes")
        continue_if_invalid = getattr(focus, 'continue_if_invalid', False)
        if continue_if_invalid:
            lines.append(f"{ind}{self.indent}continue_if_invalid = yes")

        # Tooltips
        complete_tooltip = getattr(focus, 'complete_tooltip', None)
        if complete_tooltip:
            lines.append(f"{ind}{self.indent}complete_tooltip = {{")
            for line in complete_tooltip.split('\n'):
                lines.append(f"{ind}{self.indent}{self.indent}{line}")
            lines.append(f"{ind}{self.indent}}}")

        # Completion reward
        if focus.completion_reward:
            lines.append(f"{ind}{self.indent}completion_reward = {{")
            for line in focus.completion_reward.split('\n'):
                lines.append(f"{ind}{self.indent}{self.indent}{line}")
            lines.append(f"{ind}{self.indent}}}")

        # AI will do
        ai_block = getattr(focus, 'ai_will_do_block', None)
        if ai_block and isinstance(ai_block, dict):
            lines.append(f"{ind}{self.indent}ai_will_do = {{")
            self._export_ai_block(ai_block, lines, depth + 2)
            lines.append(f"{ind}{self.indent}}}")
        else:
            # Backward compatibility: some payloads may store dict in ai_will_do
            if isinstance(getattr(focus, 'ai_will_do', None), dict):
                lines.append(f"{ind}{self.indent}ai_will_do = {{")
                self._export_ai_block(getattr(focus, 'ai_will_do'), lines, depth + 2)
                lines.append(f"{ind}{self.indent}}}")
            elif getattr(focus, 'ai_will_do', 1) != 1:
                lines.append(f"{ind}{self.indent}ai_will_do = {focus.ai_will_do}")

        lines.append(f"{ind}}}")

        return lines

    def _export_ai_block(self, ai_data: dict, lines: List[str], depth: int):
        """Recursively export AI will do block"""
        ind = self.indent * depth
        for key, value in ai_data.items():
            if isinstance(value, dict):
                lines.append(f"{ind}{key} = {{")
                self._export_ai_block(value, lines, depth + 1)
                lines.append(f"{ind}}}")
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"{ind}{key} = {{")
                        self._export_ai_block(item, lines, depth + 1)
                        lines.append(f"{ind}}}")
                    else:
                        lines.append(f"{ind}{key} = {self._format_value(item)}")
            else:
                lines.append(f"{ind}{key} = {self._format_value(value)}")

    def _format_value(self, value: Any) -> str:
        """Format value for export"""
        if isinstance(value, bool):
            return 'yes' if value else 'no'
        elif isinstance(value, str) and ' ' in value:
            return f'"{value}"'
        return str(value)

    # -------------------------
    # Localisation writer
    # -------------------------
    def _write_localisation(self, tree: FocusTree, project_root: str, *, language: str = 'english', filename_override: Optional[str] = None) -> str:
        loc_dir = os.path.join(project_root, 'localisation', language)
        os.makedirs(loc_dir, exist_ok=True)

        stem = filename_override or (getattr(tree, 'tree_name', None) or getattr(tree, 'id', None) or 'focus_tree')
        stem = self._slugify(stem, 'focus_tree')
        loc_path = os.path.join(loc_dir, f"{stem}_focus_l_{language}.yml")

        lines = []
        lines.append(f"l_{language}:")
        for f in getattr(tree, 'focuses', []) or []:
            name = getattr(f, 'name', '') or f.id
            desc = getattr(f, 'description', '') or ''
            # Paradox expects :0 suffix in many localisations; keep for compatibility
            lines.append(f"  {f.id}:0 \"{name}\"")
            if desc:
                lines.append(f"  {f.id}_desc:0 \"{desc}\"")

        content = '\n'.join(lines) + '\n'
        # UTF-8 with BOM recommended for localisation
        with open(loc_path, 'w', encoding='utf-8-sig') as f:
            f.write(content)
        return loc_path

    # -------------------------
    # GFX sprites and icon copying
    # -------------------------
    def _write_gfx_and_copy_icons(
        self,
        tree: FocusTree,
        project_root: str,
        *,
        project_slug: str,
        filename_override: Optional[str],
        icon_source_dirs: List[str],
    ) -> Dict[str, Any]:
        import shutil

        gfx_dir = os.path.join(project_root, 'gfx', 'interface', 'goals')
        os.makedirs(gfx_dir, exist_ok=True)

        stem = filename_override or f"{project_slug}_goals"
        stem = self._slugify(stem, f"{project_slug}_goals")
        gfx_path = os.path.join(gfx_dir, f"{stem}.gfx")

        # Collect icons used by focuses
        focuses = getattr(tree, 'focuses', []) or []
        icon_map: Dict[str, Dict[str, Any]] = {}
        for f in focuses:
            icon_ref = getattr(f, 'icon', None)
            if not icon_ref:
                continue
            sprite_name, src_path, basename = self._resolve_icon_sprite_name_and_source(icon_ref, project_slug, icon_source_dirs)
            icon_map[sprite_name] = {
                'src': src_path,
                'basename': basename,
            }

        # Write .gfx entries
        gfx_lines = []
        for sprite_name, meta in sorted(icon_map.items()):
            texture_file = meta['basename'] or os.path.basename(meta['src'] or '')
            if texture_file:
                texture_rel = f"gfx/interface/goals/{texture_file}"
            else:
                # Fallback: keep a placeholder to avoid invalid file reference
                texture_rel = f"gfx/interface/goals/{sprite_name}.png"
            gfx_lines.append(f"spriteType = {{ name = \"{sprite_name}\" texturefile = \"{texture_rel}\" }}")

        with open(gfx_path, 'w', encoding='utf-8-sig') as f:
            f.write('\n'.join(gfx_lines) + '\n')

        # Copy icon files to gfx dir
        copied: List[str] = []
        missing: List[str] = []
        for sprite_name, meta in icon_map.items():
            src = meta['src']
            if src and os.path.exists(src):
                try:
                    dst = os.path.join(gfx_dir, meta['basename'] or os.path.basename(src))
                    if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dst)):
                        shutil.copy2(src, dst)
                    copied.append(dst)
                except Exception:
                    self.logger.exception(f"Failed to copy icon '{src}' -> '{gfx_dir}'")
                    missing.append(sprite_name)
            else:
                missing.append(sprite_name)

        return {
            'gfx_path': gfx_path,
            'icons_copied': copied,
            'icons_missing': missing,
        }

    def _resolve_icon_sprite_name_and_source(self, icon_ref: str, project_slug: str, icon_source_dirs: List[str]) -> Tuple[str, Optional[str], Optional[str]]:
        """Determine sprite name and source file path for an icon reference.

        Returns (sprite_name, source_path, basename). If icon_ref already looks like a sprite id
        (starts with 'GFX_'), we keep that name and try to find a matching file by basename.
        Otherwise we fabricate a sprite name 'GFX_<project>_<basename>'.
        """
        icon_ref = str(icon_ref).strip()
        # If it's already a GFX sprite, try to deduce a file name
        if icon_ref.upper().startswith('GFX_'):
            sprite_name = icon_ref
            basename = None
            # Try to guess a file with same tail if path-like
            # No reliable way—leave basename None and let .gfx use sprite_name placeholder
        else:
            # Treat as path or bare filename
            base = os.path.basename(icon_ref)
            name, ext = os.path.splitext(base)
            sprite_name = f"GFX_{project_slug}_{self._slugify(name, name)}"
            basename = base if base else None

        # Resolve source path if basename available or icon_ref is an absolute path
        source_path: Optional[str] = None
        if os.path.isabs(icon_ref) and os.path.exists(icon_ref):
            source_path = icon_ref
            if not basename:
                basename = os.path.basename(icon_ref)
        elif basename:
            # Search provided icon source dirs
            for d in icon_source_dirs:
                candidate = os.path.join(d, basename)
                if os.path.exists(candidate):
                    source_path = candidate
                    break
            # Fallback: current working directory
            if not source_path:
                candidate = os.path.join(os.getcwd(), basename)
                if os.path.exists(candidate):
                    source_path = candidate

        return sprite_name, source_path, basename

# endregion

# region State File Exporter

class HOI4StateExporter:
    """Exporter for HOI4 state files (.txt)"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.indent = "\t"

    def export_state(self, state_id: str, state_meta: dict, filepath: str) -> bool:
        """Export a single state to HOI4 .txt format

        Args:
            state_id: The state ID (e.g., "1", "42")
            state_meta: Dict containing state metadata from StateViewportDock
            filepath: Output file path

        Returns:
            True if export succeeded, False otherwise
        """
        try:
            content = self.state_to_string(state_id, state_meta)
            with open(filepath, 'w', encoding='utf-8-sig') as f:
                f.write(content)
            return True
        except Exception as e:
            self.logger.error(f"Failed to export state {state_id}: {e}")
            return False

    def export_states_batch(self, states: dict[str, dict], output_dir: str) -> tuple[int, int]:
        """Export multiple states to individual .txt files

        Args:
            states: Dict mapping state_id -> state_meta
            output_dir: Directory to write state files

        Returns:
            Tuple of (success_count, fail_count)
        """
        os.makedirs(output_dir, exist_ok=True)
        success = 0
        failed = 0

        for state_id, meta in states.items():
            filepath = os.path.join(output_dir, f"{state_id}-{self._sanitize_filename(meta.get('name', 'unnamed'))}.txt")
            if self.export_state(state_id, meta, filepath):
                success += 1
            else:
                failed += 1

        return success, failed

    def state_to_string(self, state_id: str, state_meta: dict) -> str:
        """Convert state metadata to HOI4 state file format"""
        lines = []

        # State header
        lines.append(f"state = {{")
        lines.append(f"{self.indent}id = {state_id}")

        # Name
        name = state_meta.get('name', f'STATE_{state_id}')
        lines.append(f"{self.indent}name = \"{name}\"")
        lines.append("")

        # Manpower
        manpower = state_meta.get('manpower')
        if manpower is not None:
            lines.append(f"{self.indent}manpower = {manpower}")
        lines.append("")

        # State category
        state_category = state_meta.get('state_category', 'rural')
        lines.append(f"{self.indent}state_category = {state_category}")
        lines.append("")

        # Resources
        resources = state_meta.get('resources', {})
        if resources:
            lines.append(f"{self.indent}resources = {{")
            for resource_key, amount in resources.items():
                if amount and amount > 0:
                    lines.append(f"{self.indent}{self.indent}{resource_key} = {amount}")
            lines.append(f"{self.indent}}}")
            lines.append("")

        # History block
        lines.append(f"{self.indent}history = {{")

        # Owner
        owner = state_meta.get('owner')
        if owner:
            lines.append(f"{self.indent}{self.indent}owner = {owner}")

        # Cores
        cores = state_meta.get('cores', [])
        for core in cores:
            if core:
                lines.append(f"{self.indent}{self.indent}add_core_of = {core}")

        # Claims
        claims = state_meta.get('claims', [])
        for claim in claims:
            if claim:
                lines.append(f"{self.indent}{self.indent}add_claim_by = {claim}")

        # Buildings (placeholder - could be extended)
        lines.append(f"{self.indent}{self.indent}buildings = {{")
        lines.append(f"{self.indent}{self.indent}{self.indent}infrastructure = 1")
        lines.append(f"{self.indent}{self.indent}}}")

        lines.append(f"{self.indent}}}")
        lines.append("")

        # Provinces
        provinces = state_meta.get('provinces', [])
        if provinces:
            lines.append(f"{self.indent}provinces = {{")
            # Format provinces in rows of 10 for readability
            province_strs = [str(p) for p in provinces]
            for i in range(0, len(province_strs), 10):
                chunk = province_strs[i:i+10]
                lines.append(f"{self.indent}{self.indent}{' '.join(chunk)}")
            lines.append(f"{self.indent}}}")

        lines.append("}")

        return '\n'.join(lines)

    def _sanitize_filename(self, name: str) -> str:
        """Sanitize state name for use in filename"""
        # Remove or replace invalid filename characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            name = name.replace(char, '_')
        # Limit length
        return name[:50] if len(name) > 50 else name

# endregion