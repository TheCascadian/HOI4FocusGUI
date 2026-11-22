"""Versioned, recoverable project-document layer.

This module owns the *native project file format* only: reading it,
validating it, migrating it, and writing it to disk without ever leaving a
half-written file behind. HOI4 script import/export is deliberately not
touched here.

It is intentionally free of any Qt import so it can be unit-tested (and
reasoned about) without a QApplication. The only Qt-aware piece is the
recovery dialog in ``_dialog.py``, which consumes the plain dataclasses
defined below.

Document shape (schema_version 1)
---------------------------------

    {
      "schema_version": 1,
      "app_version": "1.0.9",
      "focus_tree":  {"tree_id", "country_tag", "focuses", "library", "icon_library"},
      "events":      {"events", "event_focus_links", "event_event_links"},
      "annotations": {"note_defaults", "notes", "note_connections",
                      "note_focus_links", "note_event_links"},
      "canvas":      {"settings", "viewport"},
      "metadata":    {"created_utc", "modified_utc", "generator"},
      "editor":      {"preferences", "keybindings", "state_viewport"},
      "extensions":  {...}
    }

``extensions`` carries every top-level key we did not recognise, so a file
written by a newer build (or by a plugin) survives a load/save round trip
through an older one.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    'SCHEMA_VERSION',
    'ValidationIssue',
    'ProjectDocumentError',
    'ProjectDocument',
    'detect_schema_version',
    'migrate',
    'validate',
    'dumps',
    'save_document',
    'load_document',
    'RecoveryCandidate',
    'find_recovery_candidates',
    'best_recovery_candidate',
    'ProjectSummary',
    'read_summary',
    'set_editor_preference',
    'new_document',
    'MIGRATIONS',
]

#: Bump this whenever the on-disk layout changes in a way that needs a
#: migration function. Every bump requires a new entry in MIGRATIONS.
SCHEMA_VERSION = 1

#: Number of rotating ``<name>.bak<N>`` files kept beside a saved project.
#: ``.bak1`` is always the copy of the document that was on disk immediately
#: before the current save; ``.bak<KEEP>`` is the oldest retained one.
#: Backups are only produced for explicit saves of an existing file -- a
#: first-ever save has nothing to back up, and autosaves rotate separately.
DEFAULT_BACKUP_KEEP = 5

#: Number of rotating autosave files (``<name>.json``, ``.json.1`` ...).
DEFAULT_AUTOSAVE_KEEP = 6

_SECTION_DEFAULTS: Dict[str, Callable[[], Dict[str, Any]]] = {
    'focus_tree': lambda: {
        'tree_id': '',
        'country_tag': '',
        'focuses': [],
        'library': {},
        'icon_library': {},
    },
    'events': lambda: {
        'events': [],
        'event_focus_links': [],
        'event_event_links': [],
    },
    'annotations': lambda: {
        'note_defaults': {},
        'notes': [],
        'note_connections': [],
        'note_focus_links': [],
        'note_event_links': [],
    },
    'canvas': lambda: {
        'settings': {},
        'viewport': {},
    },
    'metadata': lambda: {
        'created_utc': '',
        'modified_utc': '',
        'generator': 'HOI4FocusGUI',
    },
    'editor': lambda: {
        'preferences': {},
        'keybindings': {},
        'state_viewport': {},
    },
}

SECTION_NAMES: Tuple[str, ...] = tuple(_SECTION_DEFAULTS.keys())

# Top-level keys understood by the legacy (schema_version-less) format. Any
# other top-level key in a legacy file is preserved verbatim under
# ``extensions`` -- this is how 'states'/'provinces' from the State Viewport,
# and anything a future build adds, survive a round trip.
_LEGACY_KNOWN_KEYS = frozenset({
    'version', 'app_version', 'tree_id', 'country_tag', 'focuses', 'library',
    'icon_library', 'settings', 'note_defaults', 'notes', 'note_connections',
    'note_focus_links', 'note_event_links', 'events', 'event_focus_links',
    'event_event_links',
})


# --------------------------------------------------------------------------
# Errors and validation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationIssue:
    """A single problem, addressed by its exact path within the document."""

    path: str
    value: Any
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} (got {_short_repr(self.value)})"


def _short_repr(value: Any, limit: int = 80) -> str:
    try:
        text = repr(value)
    except Exception:
        text = f"<unrepresentable {type(value).__name__}>"
    if len(text) > limit:
        text = text[: limit - 3] + '...'
    return text


class ProjectDocumentError(Exception):
    """Raised when a document cannot be parsed, validated, or migrated.

    Carries the structured issues so the recovery dialog can explain
    precisely which field was wrong rather than just "invalid file".
    """

    def __init__(self, message: str, issues: Optional[Sequence[ValidationIssue]] = None,
                 path: Optional[str] = None):
        self.issues: List[ValidationIssue] = list(issues or ())
        self.path = path
        detail = '; '.join(str(i) for i in self.issues[:5])
        if detail:
            message = f"{message} -- {detail}"
            if len(self.issues) > 5:
                message += f" (+{len(self.issues) - 5} more)"
        super().__init__(message)


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

@dataclass
class ProjectDocument:
    """An in-memory, already-validated project document."""

    app_version: str = ''
    focus_tree: Dict[str, Any] = field(default_factory=_SECTION_DEFAULTS['focus_tree'])
    events: Dict[str, Any] = field(default_factory=_SECTION_DEFAULTS['events'])
    annotations: Dict[str, Any] = field(default_factory=_SECTION_DEFAULTS['annotations'])
    canvas: Dict[str, Any] = field(default_factory=_SECTION_DEFAULTS['canvas'])
    metadata: Dict[str, Any] = field(default_factory=_SECTION_DEFAULTS['metadata'])
    editor: Dict[str, Any] = field(default_factory=_SECTION_DEFAULTS['editor'])
    extensions: Dict[str, Any] = field(default_factory=dict)

    #: Schema versions crossed while loading this document, e.g. [0, 1].
    #: Empty when the file was already current.
    migrated_from: Optional[int] = None

    # -- conversion -------------------------------------------------------

    def to_json_obj(self) -> Dict[str, Any]:
        obj: Dict[str, Any] = {
            'schema_version': SCHEMA_VERSION,
            'app_version': self.app_version,
        }
        for name in SECTION_NAMES:
            obj[name] = getattr(self, name)
        obj['extensions'] = self.extensions
        return obj

    @classmethod
    def from_json_obj(cls, obj: Dict[str, Any]) -> 'ProjectDocument':
        """Build from an object already at the current schema version.

        Assumes ``validate()`` has passed; missing optional keys are filled
        with their defaults so callers never have to guard for them.
        """
        doc = cls(app_version=str(obj.get('app_version') or ''))
        for name in SECTION_NAMES:
            merged = _SECTION_DEFAULTS[name]()
            section = obj.get(name)
            if isinstance(section, dict):
                merged.update(section)
            setattr(doc, name, merged)
        ext = obj.get('extensions')
        doc.extensions = dict(ext) if isinstance(ext, dict) else {}
        return doc

    @classmethod
    def from_legacy(cls, project: Dict[str, Any], app_version: str = '') -> 'ProjectDocument':
        """Fold a flat, schema-version-less project dict into sections."""
        return cls.from_json_obj(_migrate_v0_to_v1(dict(project), app_version=app_version))

    def to_legacy(self) -> Dict[str, Any]:
        """Flatten back to the shape ``load_project_from_dict()`` consumes.

        The GUI still speaks the flat dialect internally; this keeps the
        document layer as a pure edge concern rather than a rewrite of the
        whole editor.
        """
        flat: Dict[str, Any] = {}
        # Unknown/plugin data first, so recognised sections always win on a
        # key collision.
        flat.update(self.extensions)

        flat['version'] = self.app_version or ''
        flat['tree_id'] = self.focus_tree.get('tree_id', '')
        flat['country_tag'] = self.focus_tree.get('country_tag', '')
        flat['focuses'] = self.focus_tree.get('focuses', [])
        flat['library'] = self.focus_tree.get('library', {})
        flat['icon_library'] = self.focus_tree.get('icon_library', {})

        flat['events'] = self.events.get('events', [])
        flat['event_focus_links'] = self.events.get('event_focus_links', [])
        flat['event_event_links'] = self.events.get('event_event_links', [])

        flat['note_defaults'] = self.annotations.get('note_defaults', {})
        flat['notes'] = self.annotations.get('notes', [])
        flat['note_connections'] = self.annotations.get('note_connections', [])
        flat['note_focus_links'] = self.annotations.get('note_focus_links', [])
        flat['note_event_links'] = self.annotations.get('note_event_links', [])

        settings: Dict[str, Any] = {}
        if self.canvas.get('settings'):
            settings['canvas'] = self.canvas.get('settings')
        if self.canvas.get('viewport'):
            settings['viewport'] = self.canvas.get('viewport')
        if self.editor.get('preferences'):
            settings['preferences'] = self.editor.get('preferences')
        if self.editor.get('keybindings'):
            settings['keybindings'] = self.editor.get('keybindings')
        if self.editor.get('state_viewport'):
            settings['state_viewport'] = self.editor.get('state_viewport')
        flat['settings'] = settings
        return flat

    def touch(self, app_version: Optional[str] = None) -> None:
        """Stamp modification metadata immediately before a save."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if app_version:
            self.app_version = str(app_version)
        if not self.metadata.get('created_utc'):
            self.metadata['created_utc'] = now
        self.metadata['modified_utc'] = now
        self.metadata.setdefault('generator', 'HOI4FocusGUI')


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def detect_schema_version(obj: Any) -> int:
    """Return the schema version of a parsed JSON object.

    Every project format that predates this module -- the flat dict written
    by older builds, the ``new_project()`` stub, and ``imported_txt_v1``
    payloads -- lacks ``schema_version`` entirely and is therefore version 0.
    """
    if not isinstance(obj, dict):
        raise ProjectDocumentError(
            'Project file must contain a JSON object',
            [ValidationIssue('$', obj, f'expected object, found {type(obj).__name__}')],
        )
    raw = obj.get('schema_version')
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProjectDocumentError(
            'Invalid schema_version',
            [ValidationIssue('schema_version', raw, 'expected an integer')],
        )
    return raw


def _migrate_v0_to_v1(obj: Dict[str, Any], app_version: str = '') -> Dict[str, Any]:
    """Flat legacy dict -> sectioned schema_version 1.

    Handles all three v0 dialects found in the repository, which differ only
    by which keys they omit:
      * full save payload (``_build_project_payload``)
      * ``new_project()`` stub: version/tree_id/country_tag/focuses/library
      * ``imported_txt_v1``: same keys, with a non-numeric ``version`` string
    """
    settings = obj.get('settings')
    settings = settings if isinstance(settings, dict) else {}

    legacy_version = obj.get('version')
    # 'imported_txt_v1' and friends are provenance markers, not app versions.
    if isinstance(legacy_version, str) and re.match(r'^\d+(\.\d+)*$', legacy_version.strip()):
        resolved_version = legacy_version.strip()
    else:
        resolved_version = str(obj.get('app_version') or app_version or '')

    out: Dict[str, Any] = {
        'schema_version': 1,
        'app_version': resolved_version,
        'focus_tree': {
            'tree_id': obj.get('tree_id', '') or '',
            'country_tag': obj.get('country_tag', '') or '',
            'focuses': obj.get('focuses', []) or [],
            'library': obj.get('library', {}) or {},
            'icon_library': obj.get('icon_library', {}) or {},
        },
        'events': {
            'events': obj.get('events', []) or [],
            'event_focus_links': obj.get('event_focus_links', []) or [],
            'event_event_links': obj.get('event_event_links', []) or [],
        },
        'annotations': {
            'note_defaults': obj.get('note_defaults', {}) or {},
            'notes': obj.get('notes', []) or [],
            'note_connections': obj.get('note_connections', []) or [],
            'note_focus_links': obj.get('note_focus_links', []) or [],
            'note_event_links': obj.get('note_event_links', []) or [],
        },
        'canvas': {
            'settings': settings.get('canvas', {}) or {},
            'viewport': settings.get('viewport', {}) or {},
        },
        'metadata': {
            'created_utc': '',
            'modified_utc': '',
            'generator': 'HOI4FocusGUI',
        },
        'editor': {
            'preferences': settings.get('preferences', {}) or {},
            'keybindings': settings.get('keybindings', {}) or {},
            'state_viewport': settings.get('state_viewport', {}) or {},
        },
    }

    # Anything we did not claim above is carried through untouched.
    extensions = {k: v for k, v in obj.items() if k not in _LEGACY_KNOWN_KEYS}
    # Same for unrecognised sub-keys of the old 'settings' blob.
    leftover_settings = {
        k: v for k, v in settings.items()
        if k not in ('canvas', 'viewport', 'preferences', 'keybindings', 'state_viewport')
    }
    if leftover_settings:
        extensions.setdefault('settings', {}).update(leftover_settings)
    # A non-numeric legacy 'version' marker is provenance worth keeping.
    if isinstance(legacy_version, str) and legacy_version.strip() and legacy_version.strip() != resolved_version:
        out['metadata']['legacy_version'] = legacy_version.strip()
    out['extensions'] = extensions
    return out


#: version -> function producing the next version. Keyed by the version being
#: migrated *from*; each entry advances exactly one step.
MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    0: _migrate_v0_to_v1,
}


def migrate(obj: Dict[str, Any]) -> Tuple[Dict[str, Any], List[int]]:
    """Advance ``obj`` to ``SCHEMA_VERSION``.

    Returns the migrated object and the list of versions it passed through.
    Never mutates the input.
    """
    version = detect_schema_version(obj)
    if version > SCHEMA_VERSION:
        raise ProjectDocumentError(
            'Project was written by a newer version of HOI4 Focus GUI',
            [ValidationIssue('schema_version', version,
                             f'this build understands up to schema_version {SCHEMA_VERSION}')],
        )
    current = json.loads(json.dumps(obj))  # deep copy, and proves it is JSON data
    steps: List[int] = []
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ProjectDocumentError(
                'No migration available',
                [ValidationIssue('schema_version', version,
                                 f'cannot migrate schema_version {version} to {SCHEMA_VERSION}')],
            )
        steps.append(version)
        current = step(current)
        next_version = detect_schema_version(current)
        if next_version <= version:
            raise ProjectDocumentError(
                'Migration did not advance the schema version',
                [ValidationIssue('schema_version', next_version,
                                 f'migration from {version} produced {next_version}')],
            )
        version = next_version
    return current, steps


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_str_list(value: Any, path: str, issues: List[ValidationIssue]) -> None:
    if not isinstance(value, list):
        issues.append(ValidationIssue(path, value, 'expected a list'))
        return
    for i, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(ValidationIssue(f'{path}[{i}]', item, 'expected a string'))


def _check_id_list(value: Any, path: str, issues: List[ValidationIssue],
                   label: str) -> None:
    """Validate a list of ``{"id": ...}`` records (focuses, events, notes)."""
    if not isinstance(value, list):
        issues.append(ValidationIssue(path, value, 'expected a list'))
        return
    seen: Dict[str, int] = {}
    for i, item in enumerate(value):
        item_path = f'{path}[{i}]'
        if not isinstance(item, dict):
            issues.append(ValidationIssue(item_path, item, f'expected a {label} object'))
            continue
        raw_id = item.get('id')
        if not isinstance(raw_id, str) or not raw_id.strip():
            issues.append(ValidationIssue(f'{item_path}.id', raw_id,
                                          'expected a non-empty string id'))
        else:
            if raw_id in seen:
                issues.append(ValidationIssue(
                    f'{item_path}.id', raw_id,
                    f'duplicate {label} id (first seen at {path}[{seen[raw_id]}])'))
            else:
                seen[raw_id] = i
        for coord in ('x', 'y'):
            if coord in item and not _is_number(item[coord]):
                issues.append(ValidationIssue(f'{item_path}.{coord}', item[coord],
                                              'expected a number'))


def validate(obj: Any) -> List[ValidationIssue]:
    """Return every problem found in a *current-schema* document.

    An empty list means the document is safe to apply to the editor. Call
    this after ``migrate()``, never before.
    """
    issues: List[ValidationIssue] = []
    if not isinstance(obj, dict):
        return [ValidationIssue('$', obj, f'expected object, found {type(obj).__name__}')]

    version = obj.get('schema_version')
    if isinstance(version, bool) or not isinstance(version, int):
        issues.append(ValidationIssue('schema_version', version, 'expected an integer'))
    elif version != SCHEMA_VERSION:
        issues.append(ValidationIssue('schema_version', version,
                                      f'expected {SCHEMA_VERSION} after migration'))

    if not isinstance(obj.get('app_version', ''), str):
        issues.append(ValidationIssue('app_version', obj.get('app_version'),
                                      'expected a string'))

    for name in SECTION_NAMES:
        if name in obj and not isinstance(obj[name], dict):
            issues.append(ValidationIssue(name, obj[name], 'expected an object'))
    if 'extensions' in obj and not isinstance(obj['extensions'], dict):
        issues.append(ValidationIssue('extensions', obj['extensions'], 'expected an object'))

    tree = obj.get('focus_tree')
    if isinstance(tree, dict):
        for key in ('tree_id', 'country_tag'):
            if key in tree and not isinstance(tree[key], str):
                issues.append(ValidationIssue(f'focus_tree.{key}', tree[key],
                                              'expected a string'))
        if 'focuses' not in tree:
            issues.append(ValidationIssue('focus_tree.focuses', None, 'required key is missing'))
        else:
            _check_id_list(tree['focuses'], 'focus_tree.focuses', issues, 'focus')
            if isinstance(tree['focuses'], list):
                for i, focus in enumerate(tree['focuses']):
                    if not isinstance(focus, dict):
                        continue
                    if 'prerequisites' in focus:
                        _check_str_list(focus['prerequisites'],
                                        f'focus_tree.focuses[{i}].prerequisites', issues)
                    if 'mutually_exclusive' in focus:
                        _check_str_list(focus['mutually_exclusive'],
                                        f'focus_tree.focuses[{i}].mutually_exclusive', issues)
                    groups = focus.get('prerequisites_groups')
                    if groups is not None:
                        if not isinstance(groups, list):
                            issues.append(ValidationIssue(
                                f'focus_tree.focuses[{i}].prerequisites_groups', groups,
                                'expected a list of groups'))
                        else:
                            for g, group in enumerate(groups):
                                _check_prereq_group(
                                    group,
                                    f'focus_tree.focuses[{i}].prerequisites_groups[{g}]',
                                    issues)
        for key in ('library', 'icon_library'):
            if key in tree and not isinstance(tree[key], dict):
                issues.append(ValidationIssue(f'focus_tree.{key}', tree[key],
                                              'expected an object'))
    elif tree is None:
        issues.append(ValidationIssue('focus_tree', None, 'required section is missing'))
    # A present-but-wrong-typed focus_tree was already reported above.

    events = obj.get('events')
    if isinstance(events, dict):
        _check_id_list(events.get('events', []), 'events.events', issues, 'event')
        for key in ('event_focus_links', 'event_event_links'):
            _check_link_pairs(events.get(key, []), f'events.{key}', issues)

    annotations = obj.get('annotations')
    if isinstance(annotations, dict):
        _check_id_list(annotations.get('notes', []), 'annotations.notes', issues, 'note')
        for key in ('note_focus_links', 'note_event_links'):
            _check_link_pairs(annotations.get(key, []), f'annotations.{key}', issues)
        if not isinstance(annotations.get('note_defaults', {}), dict):
            issues.append(ValidationIssue('annotations.note_defaults',
                                          annotations.get('note_defaults'),
                                          'expected an object'))
        if not isinstance(annotations.get('note_connections', []), list):
            issues.append(ValidationIssue('annotations.note_connections',
                                          annotations.get('note_connections'),
                                          'expected a list'))

    canvas = obj.get('canvas')
    if isinstance(canvas, dict):
        for key in ('settings', 'viewport'):
            if key in canvas and not isinstance(canvas[key], dict):
                issues.append(ValidationIssue(f'canvas.{key}', canvas[key],
                                              'expected an object'))

    editor = obj.get('editor')
    if isinstance(editor, dict):
        for key in ('preferences', 'keybindings', 'state_viewport'):
            if key in editor and not isinstance(editor[key], dict):
                issues.append(ValidationIssue(f'editor.{key}', editor[key],
                                              'expected an object'))

    metadata = obj.get('metadata')
    if isinstance(metadata, dict):
        for key in ('created_utc', 'modified_utc', 'generator'):
            if key in metadata and not isinstance(metadata[key], str):
                issues.append(ValidationIssue(f'metadata.{key}', metadata[key],
                                              'expected a string'))
    return issues


def _check_prereq_group(group: Any, path: str, issues: List[ValidationIssue]) -> None:
    """A prerequisite group is canonically ``{"type": "AND"|"OR", "items": [id]}``.

    ``_txt_converter.normalize`` also accepts a bare list of ids and a bare id
    string and folds them into that shape, so both are valid on disk too --
    rejecting them here would make previously-loadable projects unloadable.
    """
    if isinstance(group, str):
        if not group:
            issues.append(ValidationIssue(path, group, 'expected a non-empty focus id'))
        return
    if isinstance(group, list):
        _check_str_list(group, path, issues)
        return
    if not isinstance(group, dict):
        issues.append(ValidationIssue(
            path, group, 'expected {"type", "items"}, a list of ids, or an id string'))
        return
    group_type = group.get('type')
    if group_type is not None and not isinstance(group_type, str):
        issues.append(ValidationIssue(f'{path}.type', group_type, 'expected a string'))
    elif isinstance(group_type, str) and group_type.upper() not in ('AND', 'OR', ''):
        issues.append(ValidationIssue(f'{path}.type', group_type,
                                      "expected 'AND' or 'OR'"))
    if 'items' not in group:
        issues.append(ValidationIssue(f'{path}.items', None, 'required key is missing'))
    else:
        _check_str_list(group['items'], f'{path}.items', issues)


def _check_link_pairs(value: Any, path: str, issues: List[ValidationIssue]) -> None:
    """Link tables are serialised as ``[[a_id, b_id], ...]``."""
    if not isinstance(value, list):
        issues.append(ValidationIssue(path, value, 'expected a list'))
        return
    for i, pair in enumerate(value):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            issues.append(ValidationIssue(f'{path}[{i}]', pair,
                                          'expected a two-element [a, b] pair'))
            continue
        for j, endpoint in enumerate(pair):
            if not isinstance(endpoint, str):
                issues.append(ValidationIssue(f'{path}[{i}][{j}]', endpoint,
                                              'expected a string id'))


# --------------------------------------------------------------------------
# Deterministic serialization
# --------------------------------------------------------------------------

def _canonical(value: Any, path: str = '$') -> Any:
    """Normalise a value so equal documents always serialise identically.

    Dict keys become strings and are ordered by ``json.dumps(sort_keys=True)``;
    list order is meaningful and preserved; ``-0.0`` collapses to ``0.0`` and
    integral floats collapse to ints so a save/reload/save cycle is stable.
    """
    if isinstance(value, dict):
        return {str(k): _canonical(v, f'{path}.{k}') for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v, f'{path}[{i}]') for i, v in enumerate(value)]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ProjectDocumentError(
                'Project contains a value JSON cannot represent',
                [ValidationIssue(path, value, 'expected a finite number')],
            )
        if value == 0.0:
            return 0.0
        if value.is_integer() and abs(value) < 2 ** 53:
            return int(value)
        return value
    raise ProjectDocumentError(
        'Project contains a value JSON cannot represent',
        [ValidationIssue(path, value, f'unsupported type {type(value).__name__}')],
    )


def dumps(document: Any) -> str:
    """Serialise deterministically: same content in, byte-identical text out."""
    obj = document.to_json_obj() if isinstance(document, ProjectDocument) else document
    return json.dumps(
        _canonical(obj),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ': '),
    ) + '\n'


# --------------------------------------------------------------------------
# Atomic writing / rotating backups
# --------------------------------------------------------------------------

def _fsync_directory(directory: str) -> None:
    """Persist a rename in the directory entry itself, where supported.

    POSIX needs this for the replace to survive a power cut; Windows has no
    directory fd to open, so the O_DIRECTORY open simply fails and we skip.
    """
    try:
        fd = os.open(directory, getattr(os, 'O_DIRECTORY', os.O_RDONLY))
    except (AttributeError, OSError, NotImplementedError):
        return
    try:
        os.fsync(fd)
    except (OSError, NotImplementedError):
        pass
    finally:
        os.close(fd)


def atomic_write_text(target_path: str, text: str) -> None:
    """Write ``text`` to ``target_path`` so readers see old or new, never half.

    The temporary file is created *in the destination directory* so the final
    ``os.replace`` is a same-filesystem rename, which is atomic. The file is
    fsynced before the rename and the directory after it.
    """
    directory = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.focus_tmp_', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
        tmp_path = None
        _fsync_directory(directory)
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def rotate_backups(target_path: str, keep: int = DEFAULT_BACKUP_KEEP) -> None:
    """Shift ``<target>.bak1..bakN`` down and copy the live file into ``.bak1``.

    Retention: at most ``keep`` backups survive; ``.bak1`` is the newest
    (the state just before the current save) and ``.bak<keep>`` the oldest.
    ``.bak<keep>`` is deleted on each rotation. Called *before* the new
    document replaces the old one, and skipped when there is nothing to copy.
    """
    keep = max(0, int(keep))
    if keep == 0 or not os.path.isfile(target_path):
        return
    for index in range(keep, 1, -1):
        src = f'{target_path}.bak{index - 1}'
        dst = f'{target_path}.bak{index}'
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        with open(target_path, 'rb') as source:
            payload = source.read()
        directory = os.path.dirname(os.path.abspath(target_path))
        fd, tmp_path = tempfile.mkstemp(prefix='.focus_bak_', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, f'{target_path}.bak1')
            tmp_path = None
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except OSError:
        # A failed backup must never block the save itself.
        pass


def save_document(path: str, document: ProjectDocument, *,
                  app_version: Optional[str] = None,
                  backups: bool = True,
                  keep: int = DEFAULT_BACKUP_KEEP) -> str:
    """Serialise, rotate backups, and atomically replace ``path``.

    Serialisation happens *first*: if the document cannot be represented,
    nothing on disk is touched. Returns the text that was written.
    """
    document.touch(app_version=app_version)
    text = dumps(document)
    if backups:
        rotate_backups(path, keep=keep)
    atomic_write_text(path, text)
    return text


def save_rotating(base_path: str, document: ProjectDocument, *,
                  app_version: Optional[str] = None,
                  keep: int = DEFAULT_AUTOSAVE_KEEP) -> str:
    """Autosave writer: ``base`` is newest, ``base.1`` .. ``base.<keep-1>`` older.

    Retention mirrors ``rotate_backups`` but uses numeric suffixes so the
    recovery scanner can enumerate the whole generation set from one glob.
    """
    keep = max(1, int(keep))
    document.touch(app_version=app_version)
    text = dumps(document)
    directory = os.path.dirname(os.path.abspath(base_path))
    os.makedirs(directory, exist_ok=True)
    for index in range(keep - 1, 0, -1):
        src = base_path if index == 1 else f'{base_path}.{index - 1}'
        dst = f'{base_path}.{index}'
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    atomic_write_text(base_path, text)
    return text


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def loads(text: str, *, source: Optional[str] = None) -> ProjectDocument:
    """Parse -> migrate -> validate. Raises ProjectDocumentError on any failure.

    The caller receives either a fully usable document or an exception; there
    is no partial result, which is what lets the GUI leave the live project
    untouched until this returns.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectDocumentError(
            'Project file is not valid JSON',
            [ValidationIssue(f'$ (line {exc.lineno}, column {exc.colno})', None, exc.msg)],
            path=source,
        ) from exc

    original_version = detect_schema_version(raw)
    migrated, steps = migrate(raw)
    issues = validate(migrated)
    if issues:
        raise ProjectDocumentError('Project file failed validation', issues, path=source)
    doc = ProjectDocument.from_json_obj(migrated)
    doc.migrated_from = original_version if steps else None
    return doc


def load_document(path: str) -> ProjectDocument:
    """Read and fully validate the document at ``path``."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            text = handle.read()
    except OSError as exc:
        raise ProjectDocumentError(f'Could not read project file: {exc.strerror or exc}',
                                   [], path=path) from exc
    return loads(text, source=path)


@dataclass
class ProjectSummary:
    """Cheap, read-only view of a project file for browsing UIs."""

    path: str
    tree_id: str
    country_tag: str
    focus_count: int
    event_count: int
    note_count: int
    prefer_app_settings: bool
    schema_version: int


def read_summary(path: str) -> Optional[ProjectSummary]:
    """Describe a project file without loading it into the editor.

    Returns None when the file is not a project we can read at all, so
    listing code can skip it. Understands both the current schema and every
    legacy layout, because it goes through the same migration path.
    """
    try:
        doc = load_document(path)
    except ProjectDocumentError:
        return None
    return ProjectSummary(
        path=path,
        tree_id=str(doc.focus_tree.get('tree_id', '') or ''),
        country_tag=str(doc.focus_tree.get('country_tag', '') or ''),
        focus_count=len(doc.focus_tree.get('focuses', []) or []),
        event_count=len(doc.events.get('events', []) or []),
        note_count=len(doc.annotations.get('notes', []) or []),
        prefer_app_settings=bool((doc.editor.get('preferences') or {}).get('prefer_app_settings', False)),
        schema_version=SCHEMA_VERSION,
    )


def set_editor_preference(path: str, key: str, value: Any,
                          app_version: str = '') -> Optional[ProjectSummary]:
    """Flip one editor preference in a project file, in place and atomically.

    Loads and validates first, so this can never turn a good project file
    into a broken one. Returns the refreshed summary, or None if the file
    could not be read.
    """
    try:
        doc = load_document(path)
    except ProjectDocumentError:
        return None
    doc.editor.setdefault('preferences', {})[key] = value
    save_document(path, doc, app_version=app_version or doc.app_version)
    return read_summary(path)


def new_document(tree_id: str, country_tag: str = 'TAG',
                 prefer_app_settings: bool = False) -> ProjectDocument:
    """An empty, valid project document."""
    doc = ProjectDocument()
    doc.focus_tree['tree_id'] = tree_id
    doc.focus_tree['country_tag'] = country_tag
    doc.editor['preferences']['prefer_app_settings'] = bool(prefer_app_settings)
    return doc


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

@dataclass
class RecoveryCandidate:
    """One file that could stand in for a project, with its verdict."""

    path: str
    kind: str            # 'original' | 'autosave' | 'backup'
    mtime: float         # POSIX timestamp, 0.0 if unavailable
    valid: bool
    document: Optional[ProjectDocument] = None
    error: Optional[ProjectDocumentError] = None

    @property
    def timestamp_text(self) -> str:
        if not self.mtime:
            return 'unknown time'
        return datetime.fromtimestamp(self.mtime).strftime('%Y-%m-%d %H:%M:%S')

    @property
    def problem_text(self) -> str:
        """Human-readable reason this candidate was rejected, or ''."""
        if self.valid or self.error is None:
            return ''
        if self.error.issues:
            return '; '.join(str(issue) for issue in self.error.issues[:3])
        return str(self.error)


def _examine(path: str, kind: str) -> RecoveryCandidate:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    try:
        return RecoveryCandidate(path=path, kind=kind, mtime=mtime, valid=True,
                                 document=load_document(path))
    except ProjectDocumentError as exc:
        return RecoveryCandidate(path=path, kind=kind, mtime=mtime, valid=False, error=exc)


def autosave_base_for(project_path: str, autosave_dir: Optional[str] = None) -> str:
    """Mirror the app's autosave naming: ``<project dir>/autosaves/<name>``."""
    directory = autosave_dir or os.path.join(os.path.dirname(project_path) or '.', 'autosaves')
    return os.path.join(directory, os.path.basename(project_path))


def find_recovery_candidates(project_path: str,
                             autosave_dir: Optional[str] = None) -> List[RecoveryCandidate]:
    """Enumerate the project plus its autosaves and backups, newest first.

    Every candidate is fully parsed and validated here, so the caller can
    distinguish "newest file" from "newest file that actually loads".
    """
    candidates: List[RecoveryCandidate] = []
    if os.path.isfile(project_path):
        candidates.append(_examine(project_path, 'original'))

    base = autosave_base_for(project_path, autosave_dir)
    autosave_dir_resolved = os.path.dirname(base)
    if os.path.isdir(autosave_dir_resolved):
        prefix = os.path.basename(base)
        for name in sorted(os.listdir(autosave_dir_resolved)):
            if name != prefix and not name.startswith(prefix + '.'):
                continue
            # Skip our own in-flight temp files.
            if name.startswith('.focus_tmp_'):
                continue
            candidates.append(_examine(os.path.join(autosave_dir_resolved, name), 'autosave'))

    project_dir = os.path.dirname(os.path.abspath(project_path))
    stem = os.path.basename(project_path)
    if os.path.isdir(project_dir):
        for name in sorted(os.listdir(project_dir)):
            if re.match(rf'^{re.escape(stem)}\.bak\d+$', name):
                candidates.append(_examine(os.path.join(project_dir, name), 'backup'))

    candidates.sort(key=lambda c: c.mtime, reverse=True)
    return candidates


def best_recovery_candidate(project_path: str,
                            autosave_dir: Optional[str] = None
                            ) -> Optional[RecoveryCandidate]:
    """The newest candidate that *validates* -- not merely the newest file.

    A truncated autosave written moments before a crash therefore loses to a
    slightly older autosave that parses cleanly.
    """
    for candidate in find_recovery_candidates(project_path, autosave_dir):
        if candidate.valid:
            return candidate
    return None
