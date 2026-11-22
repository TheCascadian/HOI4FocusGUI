# _exporter.py
"""
HOI4 Focus Tree Exporter
Converts FocusTree dataclass instances back to .txt format via the
structured Paradox writer (:mod:`_paradox_writer`), with validated,
path-safe, atomic output. Also exports HOI4 state files from edited state
metadata.
"""

import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from _dataStructs import Focus, FocusTree
from _export_validation import (
    ExportReport,
    ExportValidationError,
    PathTraversalError,
    resolve_within,
    validate_filename,
    validate_focus_id,
    validate_icon_name,
    validate_localization_key,
)
from _paradox_writer import (
    Assign,
    BlankLine,
    Bool,
    Node,
    atom,
    block,
    escape_paradox_string,
    inline_block,
    raw_lines_block,
    render,
    value_node,
)


def _blank() -> BlankLine:
    return BlankLine()


class ExportError(Exception):
    """A structural export failure that identifies the affected focus or file.

    Raised for problems that are not simple field-validation failures (those
    go into :class:`~_export_validation.ExportReport` instead) -- e.g. an I/O
    failure while writing a specific file.
    """

    def __init__(self, message: str, *, focus_id: Optional[str] = None, file: Optional[str] = None):
        self.focus_id = focus_id
        self.file = file
        located = message
        if focus_id:
            located = f"focus {focus_id!r}: {located}"
        if file:
            located = f"{located} ({file})"
        super().__init__(located)


def _atomic_write(path: str, content: str, *, encoding: str) -> None:
    """Write ``content`` to ``path`` through a temp file + atomic replace.

    The temp file lives in the same directory as the target so the final
    ``os.replace`` is a same-filesystem rename (and therefore atomic); a
    failure partway through writing never leaves a truncated or partial file
    at ``path``.
    """

    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".export_tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# region Focus Tree Exporter

class HOI4Exporter:
    """Exporter for HOI4 focus trees."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.indent = "\t"
        self.last_export_summary: Optional[Dict[str, Any]] = None
        self.last_export_report: Optional[ExportReport] = None

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

        Returns ``True`` if the export completed with no validation
        failures. Full detail -- generated files, warnings, validation
        failures, copied/skipped assets -- is available afterwards via
        :meth:`get_last_export_report`; ``get_last_export_summary`` remains
        available for callers that only need the resolved paths.

        Programming errors (bad arguments, I/O failures that are not simple
        validation problems) raise rather than being swallowed into a bare
        ``False``: callers that want a report-only interface should use
        :meth:`export`.
        """

        self.last_export_summary = None
        report, focus_path, summary = self.export(
            tree,
            destination,
            project_name=project_name,
            focus_filename=focus_filename,
            ensure_unique=ensure_unique,
            use_mod_structure=use_mod_structure,
            encoding=encoding,
            write_localisation=write_localisation,
            localisation_language=localisation_language,
            localisation_languages=localisation_languages,
            localisation_filename=localisation_filename,
            write_gfx=write_gfx,
            gfx_filename=gfx_filename,
            icon_source_dirs=icon_source_dirs,
        )
        self.last_export_summary = summary
        self.last_export_report = report
        return report.success

    def export(
        self,
        tree: FocusTree,
        destination: str,
        *,
        project_name: Optional[str] = None,
        focus_filename: Optional[str] = None,
        ensure_unique: bool = False,
        use_mod_structure: bool = True,
        encoding: str = 'utf-8-sig',
        write_localisation: bool = True,
        localisation_language: str = 'english',
        localisation_languages: Optional[List[str]] = None,
        localisation_filename: Optional[str] = None,
        write_gfx: bool = True,
        gfx_filename: Optional[str] = None,
        icon_source_dirs: Optional[List[str]] = None,
    ) -> Tuple[ExportReport, Optional[str], Dict[str, Any]]:
        """Validate and export ``tree``, returning ``(report, focus_path, summary)``.

        Validation failures on the tree id or a focus id are collected into
        the report and abort the write (an export must never emit a script
        that references or defines an unusable identifier). I/O and
        structural problems raise :class:`ExportError` naming the affected
        focus or file, rather than being caught and turned into ``False``.
        """

        report = ExportReport()

        try:
            validate_focus_id(tree.id, field_name="focus_tree.id")
        except ExportValidationError as exc:
            report.add_failure(str(exc))
            return report, None, {}

        seen_ids: Dict[str, int] = {}
        for focus in tree.focuses:
            try:
                validate_focus_id(focus.id, field_name=f"focus.id")
            except ExportValidationError as exc:
                report.add_failure(str(exc))
                continue
            seen_ids[focus.id] = seen_ids.get(focus.id, 0) + 1
        for fid, count in seen_ids.items():
            if count > 1:
                report.add_warning(f"focus id {fid!r} is used by {count} focuses")

        if not report.success:
            return report, None, {}

        focus_path, summary = self._resolve_focus_export_path(
            tree=tree,
            destination=destination,
            project_name=project_name,
            focus_filename=focus_filename,
            ensure_unique=ensure_unique,
            use_mod_structure=use_mod_structure,
            report=report,
        )
        if focus_path is None:
            return report, None, summary

        content = self.tree_to_string(tree)
        try:
            _atomic_write(focus_path, content, encoding=encoding)
        except OSError as exc:
            raise ExportError(f"failed to write focus script: {exc}", file=focus_path) from exc
        report.focus_path = focus_path
        report.add_generated_file(focus_path)
        summary['encoding'] = encoding

        project_root = summary.get('project_root')
        project_slug = summary.get('project_slug') or self._slugify(
            getattr(tree, 'tree_name', None) or getattr(tree, 'id', None) or 'focus_project', 'focus_project'
        )
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
                    report=report,
                )
                loc_paths[lang] = loc_path
                report.add_generated_file(loc_path)

            if len(loc_paths) == 1:
                summary['localisation_path'] = next(iter(loc_paths.values()))
            summary['localisation_paths'] = loc_paths
            summary['localisation_languages'] = list(loc_paths.keys())
            report.localisation_paths = loc_paths

        if project_root and write_gfx:
            gfx_info = self._write_gfx_and_copy_icons(
                tree,
                project_root,
                project_slug=project_slug,
                filename_override=gfx_filename,
                icon_source_dirs=icon_source_dirs,
                report=report,
            )
            summary.update(gfx_info)
            report.gfx_path = gfx_info.get('gfx_path')
            if report.gfx_path:
                report.add_generated_file(report.gfx_path)
            report.copied_assets.extend(gfx_info.get('icons_copied', []))
            report.skipped_assets.extend(gfx_info.get('icons_missing', []))

        return report, focus_path, summary

    def get_last_export_summary(self) -> Optional[Dict[str, Any]]:
        """Return metadata from the most recent export operation."""
        return self.last_export_summary

    def get_last_export_report(self) -> Optional[ExportReport]:
        """Return the structured :class:`ExportReport` from the last export."""
        return self.last_export_report

    def _resolve_focus_export_path(
        self,
        tree: FocusTree,
        destination: str,
        project_name: Optional[str],
        focus_filename: Optional[str],
        ensure_unique: bool,
        use_mod_structure: bool,
        report: ExportReport,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """Determine the concrete file path for the focus export.

        Every directory join goes through :func:`resolve_within`, so a
        crafted ``project_name``/``focus_filename`` (``../../etc``) cannot
        escape the destination directory.
        """

        if not destination:
            report.add_failure("destination path must be provided")
            return None, {}

        try:
            filename = self._normalize_focus_filename(focus_filename, tree)
        except ExportValidationError as exc:
            report.add_failure(str(exc))
            return None, {}

        fallback_slug = tree.tree_name or tree.id or "focus_project"
        project_slug: Optional[str] = None

        try:
            if project_name:
                base_dir = destination or os.getcwd()
                if os.path.splitext(base_dir)[1] and not os.path.isdir(base_dir):
                    base_dir = os.path.dirname(base_dir)
                if not base_dir:
                    base_dir = os.getcwd()
                base_dir = os.path.abspath(base_dir)
                project_slug = self._slugify(project_name, fallback_slug)
                project_root = resolve_within(base_dir, project_slug, field_name="project_root")
                if use_mod_structure:
                    focus_dir = resolve_within(project_root, 'common', 'national_focus', field_name="focus_dir")
                else:
                    focus_dir = project_root
                os.makedirs(focus_dir, exist_ok=True)
                requested_path = resolve_within(focus_dir, filename, field_name="focus_path")
            else:
                destination = destination or os.getcwd()
                abs_dest = os.path.abspath(destination)
                if os.path.isdir(abs_dest) or (not os.path.exists(abs_dest) and not os.path.splitext(abs_dest)[1]):
                    focus_dir = abs_dest
                    os.makedirs(focus_dir, exist_ok=True)
                    requested_path = resolve_within(focus_dir, filename, field_name="focus_path")
                else:
                    focus_dir = os.path.dirname(abs_dest) or os.getcwd()
                    focus_dir = os.path.abspath(focus_dir)
                    os.makedirs(focus_dir, exist_ok=True)
                    requested_path = abs_dest
                project_root = focus_dir
        except PathTraversalError as exc:
            report.add_failure(str(exc))
            return None, {}

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
        filename = f"{stem}{ext}"
        return validate_filename(filename, field_name="focus_filename")

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

    # -------------------------
    # Script tree construction
    # -------------------------
    def tree_to_string(self, tree: FocusTree) -> str:
        """Convert a FocusTree to deterministic HOI4 script text."""
        return render(self._tree_nodes(tree), indent=self.indent)

    def _tree_nodes(self, tree: FocusTree) -> List[Node]:
        nodes: List[Node] = []

        if tree.search_filter_priorities:
            nodes.append(Assign('search_filter_prios', block([
                Assign(str(key), value_node(value)) for key, value in tree.search_filter_priorities.items()
            ])))
            nodes.append(_blank())

        focus_tree_children: List[Node] = []
        focus_tree_children.append(Assign('id', atom(tree.id)))
        focus_tree_children.append(_blank())

        focus_tree_children.append(Assign('country', block([
            Assign('factor', value_node(tree.country_factor)),
        ])))
        focus_tree_children.append(_blank())

        if tree.is_default:
            focus_tree_children.append(Assign('default', Bool(True)))
        focus_tree_children.append(Assign('reset_on_civilwar', Bool(bool(tree.reset_on_civilwar))))
        focus_tree_children.append(_blank())

        if tree.initial_show_position:
            focus_tree_children.append(Assign('initial_show_position', block([
                Assign('focus', atom(tree.initial_show_position)),
            ])))
            focus_tree_children.append(_blank())

        for shared in tree.shared_focuses:
            focus_tree_children.append(Assign('shared_focus', atom(shared)))
        if tree.shared_focuses:
            focus_tree_children.append(_blank())

        for focus in tree.focuses:
            focus_tree_children.append(self._focus_node(focus))
            focus_tree_children.append(_blank())

        nodes.append(Assign('focus_tree', block(focus_tree_children)))
        return nodes

    def _focus_node(self, focus: Focus) -> Assign:
        children: List[Node] = []
        children.append(Assign('id', atom(focus.id)))
        if focus.icon:
            children.append(Assign('icon', atom(focus.icon)))

        relative_pos = getattr(focus, 'relative_position_id', None)
        children.append(Assign('x', value_node(focus.x)))
        children.append(Assign('y', value_node(focus.y)))
        if relative_pos:
            children.append(Assign('relative_position_id', atom(relative_pos)))

        children.append(Assign('cost', value_node(focus.cost)))

        search_filters = getattr(focus, 'search_filters', None)
        if search_filters:
            children.append(Assign('search_filters', inline_block([atom(s) for s in search_filters])))

        if getattr(focus, 'prerequisites_groups', None):
            for group in focus.prerequisites_groups:
                gtype = str(group.get('type') or '').upper().strip()
                items = list(group.get('items') or [])
                if gtype in ('AND', 'OR'):
                    inner = block([Assign(gtype, block([
                        Assign('focus', atom(item)) for item in items
                    ]))])
                    children.append(Assign('prerequisite', inner))
                else:
                    children.append(Assign('prerequisite', block([
                        Assign('focus', atom(item)) for item in items
                    ])))
        elif focus.prerequisites:
            if focus.prerequisites_grouped:
                children.append(Assign('prerequisite', block([
                    Assign('focus', atom(prereq)) for prereq in focus.prerequisites
                ])))
            else:
                for prereq in focus.prerequisites:
                    children.append(Assign('prerequisite', inline_block([Assign('focus', atom(prereq))])))

        for mutex in focus.mutually_exclusive:
            children.append(Assign('mutually_exclusive', inline_block([Assign('focus', atom(mutex))])))

        if getattr(focus, 'allow_branch', None):
            children.append(Assign('allow_branch', raw_lines_block(str(focus.allow_branch), skip_blank=True)))

        if focus.available:
            children.append(Assign('available', raw_lines_block(focus.available)))

        available_if_cap = getattr(focus, 'available_if_capitulated', False)
        if available_if_cap:
            children.append(Assign('available_if_capitulated', Bool(True)))

        if focus.bypass:
            children.append(Assign('bypass', raw_lines_block(focus.bypass)))

        if getattr(focus, 'cancel_if_invalid', False):
            children.append(Assign('cancel_if_invalid', Bool(True)))
        if getattr(focus, 'continue_if_invalid', False):
            children.append(Assign('continue_if_invalid', Bool(True)))

        complete_tooltip = getattr(focus, 'complete_tooltip', None)
        if complete_tooltip:
            children.append(Assign('complete_tooltip', raw_lines_block(complete_tooltip)))

        if focus.completion_reward:
            children.append(Assign('completion_reward', raw_lines_block(focus.completion_reward)))

        ai_block = getattr(focus, 'ai_will_do_block', None)
        if ai_block and isinstance(ai_block, dict):
            children.append(Assign('ai_will_do', block(self._ai_block_nodes(ai_block))))
        elif isinstance(getattr(focus, 'ai_will_do', None), dict):
            children.append(Assign('ai_will_do', block(self._ai_block_nodes(getattr(focus, 'ai_will_do')))))
        elif getattr(focus, 'ai_will_do', 1) != 1:
            children.append(Assign('ai_will_do', value_node(focus.ai_will_do)))

        raw_unparsed = getattr(focus, 'raw_unparsed', None) or []
        for chunk in raw_unparsed:
            text = str(chunk)
            if not text.strip():
                continue
            children.append(raw_lines_block(text))

        return Assign('focus', block(children))

    def _ai_block_nodes(self, ai_data: dict) -> List[Node]:
        nodes: List[Node] = []
        for key, value in ai_data.items():
            if isinstance(value, dict):
                nodes.append(Assign(str(key), block(self._ai_block_nodes(value))))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        nodes.append(Assign(str(key), block(self._ai_block_nodes(item))))
                    else:
                        nodes.append(Assign(str(key), value_node(item)))
            else:
                nodes.append(Assign(str(key), value_node(value)))
        return nodes

    # -------------------------
    # Localisation writer
    # -------------------------
    def _write_localisation(
        self,
        tree: FocusTree,
        project_root: str,
        *,
        language: str = 'english',
        filename_override: Optional[str] = None,
        report: ExportReport,
    ) -> str:
        loc_dir = resolve_within(project_root, 'localisation', language, field_name="localisation_dir")
        os.makedirs(loc_dir, exist_ok=True)

        stem = filename_override or (getattr(tree, 'tree_name', None) or getattr(tree, 'id', None) or 'focus_tree')
        stem = self._slugify(stem, 'focus_tree')
        loc_filename = validate_filename(f"{stem}_focus_l_{language}.yml", field_name="localisation_filename")
        loc_path = resolve_within(loc_dir, loc_filename, field_name="localisation_path")

        lines = [f"l_{language}:"]
        for f in getattr(tree, 'focuses', []) or []:
            try:
                validate_localization_key(f.id, field_name="localization key")
            except ExportValidationError as exc:
                report.add_warning(f"skipped localisation for {str(exc)}")
                continue
            name = getattr(f, 'name', '') or f.id
            desc = getattr(f, 'description', '') or ''
            lines.append(f'  {f.id}:0 "{escape_paradox_string(str(name))}"')
            if desc:
                lines.append(f'  {f.id}_desc:0 "{escape_paradox_string(str(desc))}"')

        content = '\n'.join(lines) + '\n'
        _atomic_write(loc_path, content, encoding='utf-8-sig')
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
        report: ExportReport,
    ) -> Dict[str, Any]:
        gfx_dir = resolve_within(project_root, 'gfx', 'interface', 'goals', field_name="gfx_dir")
        os.makedirs(gfx_dir, exist_ok=True)

        stem = filename_override or f"{project_slug}_goals"
        stem = self._slugify(stem, f"{project_slug}_goals")
        gfx_filename = validate_filename(f"{stem}.gfx", field_name="gfx_filename")
        gfx_path = resolve_within(gfx_dir, gfx_filename, field_name="gfx_path")

        focuses = getattr(tree, 'focuses', []) or []
        icon_map: Dict[str, Dict[str, Any]] = {}
        for f in focuses:
            icon_ref = getattr(f, 'icon', None)
            if not icon_ref:
                continue
            try:
                validate_icon_name(str(icon_ref), field_name=f"focus {f.id} icon")
            except ExportValidationError as exc:
                report.add_warning(str(exc))
                continue
            sprite_name, src_path, basename = self._resolve_icon_sprite_name_and_source(icon_ref, project_slug, icon_source_dirs)
            icon_map[sprite_name] = {
                'src': src_path,
                'basename': basename,
            }

        gfx_nodes: List[Node] = []
        for sprite_name, meta in sorted(icon_map.items()):
            texture_file = meta['basename'] or os.path.basename(meta['src'] or '')
            texture_rel = f"gfx/interface/goals/{texture_file}" if texture_file else f"gfx/interface/goals/{sprite_name}.png"
            gfx_nodes.append(Assign('spriteType', inline_block([
                Assign('name', atom(sprite_name)),
                Assign('texturefile', atom(texture_rel)),
            ])))

        gfx_content = render(gfx_nodes, indent=self.indent) if gfx_nodes else ''
        _atomic_write(gfx_path, gfx_content, encoding='utf-8-sig')

        copied: List[str] = []
        missing: List[str] = []
        for sprite_name, meta in icon_map.items():
            src = meta['src']
            if src and os.path.exists(src):
                try:
                    dst_name = meta['basename'] or os.path.basename(src)
                    dst = resolve_within(gfx_dir, dst_name, field_name="icon_destination")
                    if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dst)):
                        shutil.copy2(src, dst)
                    copied.append(dst)
                except (OSError, PathTraversalError) as exc:
                    self.logger.warning(f"Failed to copy icon '{src}' -> '{gfx_dir}': {exc}")
                    report.add_warning(f"failed to copy icon {sprite_name!r}: {exc}")
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
        if icon_ref.upper().startswith('GFX_'):
            sprite_name = icon_ref
            basename = None
        else:
            base = os.path.basename(icon_ref)
            name, ext = os.path.splitext(base)
            sprite_name = f"GFX_{project_slug}_{self._slugify(name, name)}"
            basename = base if base else None

        source_path: Optional[str] = None
        if os.path.isabs(icon_ref) and os.path.exists(icon_ref):
            source_path = icon_ref
            if not basename:
                basename = os.path.basename(icon_ref)
        elif basename:
            for d in icon_source_dirs:
                candidate = os.path.join(d, basename)
                if os.path.exists(candidate):
                    source_path = candidate
                    break
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
        except (TypeError, ValueError) as e:
            self.logger.error(f"Failed to build state {state_id}: {e}")
            return False
        _atomic_write(filepath, content, encoding='utf-8-sig')
        return True

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
        lines.append(f"{self.indent}name = \"{escape_paradox_string(str(name))}\"")
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
