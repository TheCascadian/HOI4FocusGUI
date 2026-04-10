from _imports import (
    # Project
    CommandSpec, Event, Focus, clone_focus_pure, draw_outlined_text, format_project_file_size, obfuscate_path, obfuscate_text, obfuscate_user_in_path, pixmap_from_file_via_pillow, safe_qt_call, safe_ui_operation, set_widget_path_display, show_error,
)
from error_handler import (
    ConfigurationError,
    ErrorPolicy,
    FileOperationError,
    PolicyConfig,
    SerializationError,
    configure_error_handler,
    handle_exception,
    operation_context,
    silent_operation,
)

import logging
from _imports import (
    # Standard library
    datetime, json, math, os, re, sys, time, Path,
    defaultdict, wraps, shutil, subprocess, tempfile, threading,
    uuid, weakref, suppress,
    # Typing
    Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING, Union,
)
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from PyQt6.QtWidgets import *
from _commands import (
    ColorizeNodesCommand,
    DeleteEventCommand,
    EditFocusCommand,
    MakeMutexCommand,
    MoveNodeCommand,
    SetIconCommand,
)
from _nodes import (
    ConnectionItem,
    EnhancedGraphicsView,
    EventEventConnector,
    EventFocusConnector,
    EventNode,
    FocusNode,
    LShapedConnectionLine,
    MutualExclusiveConnector,
    NodeBase,
    NoteConnectionLine,
    NoteEventConnector,
    NoteFocusConnector,
    NoteNode,
)

from _dialog import (
    EditorDialogBase,
    MultiAddDialog,
    NodePaletteDialog,
    FocusEditDialog,
    EventEditDialog,
    LayerManagerDialog,
    ProjectsHomeDialog,
    FindNotesDialog,
    ProjectNoteSettingsDialog,
    IconLibraryDialog,
    SettingsDialog,
)

from _txt_converter import convert_txt_to_project_dict


StateViewportDock = None
with silent_operation("import_StateViewportDock"):
    from _state_viewport import StateViewportDock
_write_state_sidecar = None
with silent_operation("import_write_state_sidecar"):
    # helper for writing sidecar files with one-line-per-item formatting
    from _state_viewport import _write_state_sidecar

Image = None
with silent_operation("import_PIL_Image"):
    from PIL import Image

logger = logging.getLogger(__name__)

GitHubUpdater = None
with silent_operation("import_GitHubUpdater"):
    from _updater import GitHubUpdater

#region Global UI safety patches
# Disable the default QMainWindow toolbar/dock popup menu so users cannot hide
# toolbars via right-click anywhere in the main window's toolbar area.
with silent_operation("patch_QMainWindow_createPopupMenu"):
    def _no_mainwindow_popup(self):
        return None
    QMainWindow.createPopupMenu = _no_mainwindow_popup  # type: ignore[attr-defined]
#endregion

# Performance decorators moved to _utils.py to avoid circular imports

def atomic_write_json(target_path: str, payload: Any) -> None:
    """Write JSON payload to target path atomically via temporary file rename."""
    if not target_path:
        return
    directory = os.path.dirname(target_path)
    if directory:
        with silent_operation("makedirs_for_atomic_write"):
            os.makedirs(directory, exist_ok=True)
    tmp_dir = directory if directory else None
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='focus_tmp_', suffix='.json', dir=tmp_dir)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            with silent_operation("fsync_json_file"):
                os.fsync(handle.fileno())
            fd = None  # fd already owned by handle
        os.replace(tmp_path, target_path)
    except Exception as exc:
        logger.warning('atomic_write_json failed for %s: %s', target_path, exc)
        if tmp_path:
            with suppress(Exception):
                os.remove(tmp_path)
        raise
    finally:
        if fd is not None:
            with suppress(Exception):
                os.close(fd)

def write_rotating_autosave(base_path: str, payload: Any, keep: int) -> None:
    """Write payload to base_path while maintaining up to `keep` rotating backups."""
    if not base_path:
        return
    keep = max(1, int(keep) if keep is not None else 1)
    directory = os.path.dirname(base_path)
    if directory:
        with silent_operation("makedirs_for_autosave"):
            os.makedirs(directory, exist_ok=True)
    for idx in range(keep, 0, -1):
        src = base_path if idx == 1 else f"{base_path}.{idx-1}"
        dst = f"{base_path}.{idx}"
        if os.path.exists(dst):
            with suppress(Exception):
                os.remove(dst)
        if os.path.exists(src):
            with suppress(Exception):
                os.replace(src, dst)
    atomic_write_json(base_path, payload)

"""
HOI4 Focus GUI
A visual editor for creating Hearts of Iron IV focus trees with improved UX
Includes a reusable Focus Library (dictionary) for saving/applying focus property snippets.
"""

# Keep upstream defaults, but allow forks/CI to override without patching code.
GITHUB_REPO_OWNER = os.environ.get("FOCUS_GITHUB_REPO_OWNER", "TheCascadian")
GITHUB_REPO_NAME = os.environ.get("FOCUS_GITHUB_REPO_NAME", "HOI4FocusGUI")

#region Mute App patch
# Monkeypatch QMessageBox helpers so they respect an application-wide `muted` flag
# When muted, show a lightweight custom dialog (no system notification sound) instead
try:
    _orig_qinformation = QMessageBox.information
    _orig_qwarning = QMessageBox.warning
    _orig_qcritical = QMessageBox.critical
    _orig_qqestion = QMessageBox.question

    def _map_qmb_to_qdb(qmb_button):
        # Map QMessageBox.StandardButton -> QDialogButtonBox.StandardButton where possible
        mapping = {
            QMessageBox.StandardButton.Ok: QDialogButtonBox.StandardButton.Ok,
            QMessageBox.StandardButton.Save: QDialogButtonBox.StandardButton.Save,
            QMessageBox.StandardButton.Open: QDialogButtonBox.StandardButton.Open,
            QMessageBox.StandardButton.Yes: QDialogButtonBox.StandardButton.Yes,
            QMessageBox.StandardButton.No: QDialogButtonBox.StandardButton.No,
            QMessageBox.StandardButton.Cancel: QDialogButtonBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Retry: QDialogButtonBox.StandardButton.Retry,
            QMessageBox.StandardButton.Ignore: QDialogButtonBox.StandardButton.Ignore,
            QMessageBox.StandardButton.Close: QDialogButtonBox.StandardButton.Close,
            QMessageBox.StandardButton.Apply: QDialogButtonBox.StandardButton.Apply,
            QMessageBox.StandardButton.Help: QDialogButtonBox.StandardButton.Help,
        }
        return mapping.get(qmb_button, None)

    def _silent_dialog(parent, title, text, buttons=QMessageBox.StandardButton.Ok, defaultButton=QMessageBox.StandardButton.No, modal=True):
        try:
            dlg = QDialog(parent)
            dlg.setWindowTitle(str(title))
            dlg.setModal(bool(modal))
            layout = QVBoxLayout(dlg)
            lbl = QLabel(str(text), dlg)
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            # Build QDialogButtonBox with requested buttons
            qdb_buttons = QDialogButtonBox.StandardButton(0)
            # iterate known buttons in stable order
            known = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No, QMessageBox.StandardButton.Retry, QMessageBox.StandardButton.Ignore, QMessageBox.StandardButton.Ok, QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Close, QMessageBox.StandardButton.Apply]
            for kb in known:
                try:
                    if int(buttons) & int(kb):
                        mapped = _map_qmb_to_qdb(kb)
                        if mapped is not None:
                            qdb_buttons |= mapped
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # default to Ok if nothing mapped
            if int(qdb_buttons) == 0:
                qdb_buttons = QDialogButtonBox.StandardButton.Ok
            box = QDialogButtonBox(qdb_buttons, parent=dlg)
            layout.addWidget(box)

            result_holder = {'res': QMessageBox.StandardButton.No}

            def on_clicked(bt):
                try:
                    std = box.standardButton(bt)
                    # map back to QMessageBox.StandardButton by name
                    name = std.name if hasattr(std, 'name') else None
                    # try to find a matching QMessageBox.StandardButton
                    for candidate in QMessageBox.StandardButton:
                        try:
                            if candidate.name == name:
                                result_holder['res'] = candidate
                                break
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    dlg.accept()
                except Exception:
                    try:
                        dlg.close()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            box.clicked.connect(on_clicked)
            dlg.exec()
            return result_holder['res']
        except Exception:
            # fallback: return a sensible default
            try:
                return defaultButton
            except Exception:
                return QMessageBox.StandardButton.No

    def _wrap_info(*args, **kwargs):
        try:
            parent = args[0] if args else kwargs.get('parent', None)
            title = args[1] if len(args) > 1 else kwargs.get('title', '')
            text = args[2] if len(args) > 2 else kwargs.get('text', '')
            buttons = args[3] if len(args) > 3 else kwargs.get('buttons', QMessageBox.StandardButton.Ok)
            default = args[4] if len(args) > 4 else kwargs.get('defaultButton', QMessageBox.StandardButton.No)
            if getattr(parent, 'muted', False):
                return _silent_dialog(parent, title, text, buttons=buttons, defaultButton=default)
            return _orig_qinformation(*args, **kwargs)
        except Exception:
            return _orig_qinformation(*args, **kwargs)

    def _wrap_warn(*args, **kwargs):
        try:
            parent = args[0] if args else kwargs.get('parent', None)
            title = args[1] if len(args) > 1 else kwargs.get('title', '')
            text = args[2] if len(args) > 2 else kwargs.get('text', '')
            buttons = args[3] if len(args) > 3 else kwargs.get('buttons', QMessageBox.StandardButton.Ok)
            default = args[4] if len(args) > 4 else kwargs.get('defaultButton', QMessageBox.StandardButton.No)
            if getattr(parent, 'muted', False):
                return _silent_dialog(parent, title, text, buttons=buttons, defaultButton=default)
            return _orig_qwarning(*args, **kwargs)
        except Exception:
            return _orig_qwarning(*args, **kwargs)

    def _wrap_crit(*args, **kwargs):
        try:
            parent = args[0] if args else kwargs.get('parent', None)
            title = args[1] if len(args) > 1 else kwargs.get('title', '')
            text = args[2] if len(args) > 2 else kwargs.get('text', '')
            buttons = args[3] if len(args) > 3 else kwargs.get('buttons', QMessageBox.StandardButton.Ok)
            default = args[4] if len(args) > 4 else kwargs.get('defaultButton', QMessageBox.StandardButton.No)
            if getattr(parent, 'muted', False):
                return _silent_dialog(parent, title, text, buttons=buttons, defaultButton=default)
            return _orig_qcritical(*args, **kwargs)
        except Exception:
            return _orig_qcritical(*args, **kwargs)

    def _wrap_question(*args, **kwargs):
        try:
            parent = args[0] if args else kwargs.get('parent', None)
            title = args[1] if len(args) > 1 else kwargs.get('title', '')
            text = args[2] if len(args) > 2 else kwargs.get('text', '')
            buttons = args[3] if len(args) > 3 else kwargs.get('buttons', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            default = args[4] if len(args) > 4 else kwargs.get('defaultButton', QMessageBox.StandardButton.No)
            if getattr(parent, 'muted', False):
                return _silent_dialog(parent, title, text, buttons=buttons, defaultButton=default)
            return _orig_qqestion(*args, **kwargs)
        except Exception:
            return _orig_qqestion(*args, **kwargs)

    QMessageBox.information = _wrap_info
    QMessageBox.warning = _wrap_warn
    QMessageBox.critical = _wrap_crit
    QMessageBox.question = _wrap_question
except Exception as e:
    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

#endregion

#region Game State

class GameStateDock(QDockWidget):
    """Simple dock panel to simulate game state: completed focuses and simple flags.

    Emits 'state_changed' when the set of completed focuses changes. The editor
    uses this to update node availability rendering.
    """
    state_changed = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__("Game State", parent)
        self.setObjectName('GameStateDock')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        w = QWidget(self)
        self.setWidget(w)
        layout = QVBoxLayout(w)
        # Visibility controls for categories
        vis_row = QHBoxLayout()
        self.show_hidden_chk = QCheckBox('Show Hidden')
        self.show_hidden_chk.setChecked(False)
        vis_row.addWidget(self.show_hidden_chk)
        self.show_unavail_chk = QCheckBox('Show Unavailable')
        self.show_unavail_chk.setChecked(True)
        vis_row.addWidget(self.show_unavail_chk)
        self.show_available_chk = QCheckBox('Show Available')
        self.show_available_chk.setChecked(True)
        vis_row.addWidget(self.show_available_chk)
        layout.addLayout(vis_row)
        # search/filter
        self.search = QLineEdit()
        self.search.setPlaceholderText('Filter focuses by id or name...')
        layout.addWidget(self.search)
        # list with checkboxes
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(self.list, 1)
        btn_row = QHBoxLayout()
        self.clear_btn = QPushButton('Clear Completed')
        btn_row.addWidget(self.clear_btn)
        self.check_all_btn = QPushButton('Mark All Completed')
        btn_row.addWidget(self.check_all_btn)
        layout.addLayout(btn_row)

        # internal state
        self.completed = set()

        # connections
        self.search.textChanged.connect(self._on_search)
        self.list.itemChanged.connect(self._on_item_changed)
        self.show_hidden_chk.stateChanged.connect(lambda _: self.state_changed.emit())
        self.show_unavail_chk.stateChanged.connect(lambda _: self.state_changed.emit())
        self.show_available_chk.stateChanged.connect(lambda _: self.state_changed.emit())
        self.clear_btn.clicked.connect(self._on_clear)
        self.check_all_btn.clicked.connect(self._on_check_all)

    def populate(self, focuses):
        """Populate the list with given focuses: iterable of Focus instances or dicts with id/name"""
        self.list.blockSignals(True)
        self.list.clear()
        for f in focuses:
            fid = getattr(f, 'id', None) if hasattr(f, 'id') else f.get('id') if isinstance(f, dict) else None
            title = getattr(f, 'name', '') if hasattr(f, 'name') else f.get('name', '') if isinstance(f, dict) else ''
            if not fid:
                continue
            it = QListWidgetItem(f"{fid} — {title}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if fid in self.completed else Qt.CheckState.Unchecked)
            # store id
            it.setData(Qt.ItemDataRole.UserRole, str(fid))
            self.list.addItem(it)
        self.list.blockSignals(False)

    def _on_search(self, txt):
        q = (txt or '').lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            visible = True
            if q:
                visible = q in (it.text() or '').lower()
            self.list.item(i).setHidden(not visible)

    def _on_item_changed(self, item):
        fid = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.completed.add(fid)
        else:
            self.completed.discard(fid)
        with silent_operation("emit_state_changed"):
            self.state_changed.emit()

    def _on_clear(self):
        self.completed.clear()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.CheckState.Unchecked)
        with silent_operation("emit_state_changed"):
            self.state_changed.emit()

    def _on_check_all(self):
        self.completed = set()
        for i in range(self.list.count()):
            it = self.list.item(i)
            fid = it.data(Qt.ItemDataRole.UserRole)
            self.completed.add(fid)
            it.setCheckState(Qt.CheckState.Checked)
        with silent_operation("emit_state_changed"):
            self.state_changed.emit()

    def is_completed(self, focus_id: str) -> bool:
        return str(focus_id) in self.completed

#endregion

#region Utilities

# EditorDialogBase is now in _dialog.py to avoid circular imports

#endregion

#region Keybinds

class KeybindsManager(QObject): # Keybindings: manager and editor

    """Central manager for app keybindings. Creates QShortcut objects bound to the main window.

    - Exposes commands with IDs, labels, callbacks, and default shortcuts
    - Supports load/save of current mapping as a dict of {id: 'KeySequence'} strings
    - Can be edited via the Keybinds editor panel
    """
    keybinds_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._commands: Dict[str, CommandSpec] = {}
        self._owner_widget: Optional[QWidget] = None
        # Registration order to provide deterministic priority when resolving conflicts
        self._order: List[str] = []
        # Map of normalized sequence -> list of command ids (len>1 indicates conflict)
        self._conflicts: Dict[str, List[str]] = {}
        # named scope widgets (e.g., 'state_viewport' -> QWidget) for local shortcuts
        self._scope_widgets: Dict[str, QWidget] = {}

    def set_owner(self, widget: QWidget) -> None:
        self._owner_widget = widget
    # Rebuild all shortcuts against the new owner
        self._rebuild_shortcuts()

    def register_scope_widget(self, name: str, widget: QWidget) -> None:
        """Register a named widget that can be used as a local scope for shortcuts.

        When a CommandSpec has widget_scope set to this name, its QShortcut will be
        parented on the provided widget so it only triggers when that widget (or its
        children) has focus.
        """
        with silent_operation("register_scope_widget"):
            if name and widget is not None:
                self._scope_widgets[name] = widget
                # Rebuild so any commands that requested this scope become active
                self._rebuild_shortcuts()

    def register_commands(self, commands: List[CommandSpec]) -> None:
        for spec in commands:
            self._commands[spec.cid] = spec
            if spec.cid not in self._order:
                self._order.append(spec.cid)
        # Build shortcuts once to avoid transient duplicates
        self._rebuild_shortcuts()

    def _create_or_update_qshortcut(self, spec: CommandSpec) -> None:
        if not self._owner_widget:
            return
        # destroy old
        if spec.qshortcut is not None:
            with silent_operation("disconnect_qshortcut"):
                spec.qshortcut.activated.disconnect()
            spec.qshortcut.setParent(None)
            spec.qshortcut.deleteLater()
            spec.qshortcut = None
        seq_str = spec.shortcut or spec.default
        if not seq_str:
            return
        try:
            qseq = QKeySequence(seq_str)
        except Exception:
            return
        # Determine the parent widget for this shortcut. If the command specifies a
        # widget_scope and a corresponding widget is registered, parent the QShortcut
        # on that widget and limit its context to the widget and its children.
        parent_widget = None
        try:
            ws = getattr(spec, 'widget_scope', None)
            if ws and ws in self._scope_widgets:
                parent_widget = self._scope_widgets.get(ws)
        except Exception:
            parent_widget = None
        if parent_widget is None:
            parent_widget = self._owner_widget

        sc = QShortcut(qseq, parent_widget)
        # If parent is a scope widget, use WidgetWithChildrenShortcut so it only
        # triggers when that widget (or child) has focus. Otherwise default to
        # ApplicationShortcut for global bindings.
        with silent_operation("set_shortcut_context"):
            if parent_widget is not None and getattr(spec, 'widget_scope', None):
                sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            else:
                sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(lambda s=spec: self._invoke(s))
        spec.qshortcut = sc

    def _remove_qshortcut(self, spec: CommandSpec) -> None:
        """Remove any live QShortcut for this spec."""
        with silent_operation("remove_qshortcut"):
            if spec.qshortcut is not None:
                with silent_operation("disconnect_qshortcut"):
                    spec.qshortcut.activated.disconnect()
                with silent_operation("delete_qshortcut"):
                    spec.qshortcut.setParent(None)
                    spec.qshortcut.deleteLater()
                spec.qshortcut = None

    def _normalized_seq(self, seq_str: Optional[str]) -> Optional[str]:
        if not seq_str:
            return None
        try:
            # Use PortableText to ensure cross-platform stable representation
            return QKeySequence(seq_str).toString(QKeySequence.SequenceFormat.PortableText)
        except Exception:
            return seq_str

    def _rebuild_shortcuts(self) -> None:
        """Recreate all QShortcuts in a single pass, enforcing uniqueness and tracking conflicts.

        Rule: If multiple commands share the same (normalized) key sequence, only the first
        command by registration order wins the active shortcut. Others are left without a
        QShortcut until the conflict is resolved.
        """
        # Clear any existing QShortcuts first to avoid transient ambiguities
        for spec in self._commands.values():
            self._remove_qshortcut(spec)

        # Build map of seq -> list of cids (preserving registration order)
        seq_to_cids: Dict[str, List[str]] = {}
        if self._owner_widget is None:
            self._conflicts = {}
            return
        for cid in self._order:
            spec = self._commands.get(cid)
            if not spec:
                continue
            seq_str = spec.shortcut or spec.default
            n = self._normalized_seq(seq_str)
            if not n:
                continue
            seq_to_cids.setdefault(n, []).append(cid)

        # Create winners and record conflicts
        self._conflicts = {k: v for k, v in seq_to_cids.items() if len(v) > 1}
        for nseq, cid_list in seq_to_cids.items():
            if not cid_list:
                continue
            winner = cid_list[0]  # first by registration order
            wspec = self._commands.get(winner)
            if wspec:
                # Ensure winner uses the normalized string to avoid platform diffs
                with silent_operation("set_winner_shortcut"):
                    wspec.shortcut = nseq if wspec.shortcut else wspec.shortcut
                self._create_or_update_qshortcut(wspec)
            # Losers remain without QShortcuts (already removed above)

        # Emit change signal so any UI can refresh conflict highlighting
        with silent_operation("emit_keybinds_changed"):
            self.keybinds_changed.emit()

    def _invoke(self, spec: CommandSpec) -> None:
        try:
            spec.callback()
        except Exception as e:
            with silent_operation("show_keybind_error"):
                show_error(self._owner_widget, "Keybind Error", f"Failed to execute action '{spec.label}'.", e)

    def list_commands(self) -> List[CommandSpec]:
        return list(sorted(self._commands.values(), key=lambda s: s.label.lower()))

    def get_mapping(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for cid, spec in self._commands.items():
            if spec.shortcut:
                out[cid] = spec.shortcut
        return out

    def apply_mapping(self, mapping: Dict[str, str]) -> None:
        # Assign and recreate shortcuts
        for cid, spec in self._commands.items():
            spec.shortcut = mapping.get(cid, spec.shortcut)
        self._rebuild_shortcuts()

    def set_shortcut(self, cid: str, sequence: Optional[str]) -> None:
        if cid not in self._commands:
            return
        self._commands[cid].shortcut = sequence
        self._rebuild_shortcuts()

    def reset_shortcut(self, cid: str) -> None:
        if cid not in self._commands:
            return
        self._commands[cid].shortcut = None
        self._rebuild_shortcuts()

    def get_conflicts(self) -> Dict[str, List[str]]:
        """Return current conflicts: normalized sequence -> list of command ids (len>1)."""
        return dict(self._conflicts)

class KeybindsEditorDialog(EditorDialogBase):
    """Dialog to edit keybindings using QKeySequenceEdit controls."""
    def __init__(self, manager: KeybindsManager, parent: Optional[QWidget] = None, title: Optional[str] = "Edit Keybindings", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.manager = manager
        self._edits: Dict[str, 'QKeySequenceEdit'] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        info = QLabel("Double-click a shortcut to edit. Press Delete to clear.")
        info.setWordWrap(True)
        layout.addWidget(info)
        # Use a grouped QTreeWidget: top-level items are categories, children are commands
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Action", "Shortcut", "Default"])
        self.tree.header().setStretchLastSection(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(12)
        layout.addWidget(self.tree)

        # Populate grouped tree with all commands from the manager (no filtering)
        commands = list(self.manager.list_commands())

        class _LocalSpec:
            def __init__(self, cid, label, shortcut, default, category):
                self.cid = cid
                self.label = label
                self.shortcut = shortcut
                self.default = default
                self.category = category

        normalized: List[_LocalSpec] = []
        for spec in commands:
            try:
                # Use provided category or fall back to 'General', normalize casing
                cat = (spec.category or 'General').strip()
                cat = cat.title()
                label = (spec.label or spec.cid or '').strip()
                # normalize whitespace in label
                label = ' '.join(label.split())
                norm = _LocalSpec(spec.cid, label, spec.shortcut or '', spec.default or '', cat)
                normalized.append(norm)
            except Exception:
                continue

        # Group by category
        groups: Dict[str, list] = {}
        for spec in normalized:
            groups.setdefault(spec.category or 'General', []).append(spec)

        # Sort categories for deterministic display
        for cat in sorted(groups.keys(), key=lambda s: s.lower()):
            top = QTreeWidgetItem(self.tree, [cat])
            top.setFirstColumnSpanned(True)
            top.setExpanded(True)
            # Add child items
            specs = sorted(groups[cat], key=lambda s: s.label.lower())
            for spec in specs:
                # Show 'App Default' in Default column per policy; do not surface source info
                child = QTreeWidgetItem(top, [spec.label, '', 'App Default'])
                # store cid for lookup
                child.setData(0, Qt.ItemDataRole.UserRole, spec.cid)
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                # Shortcut editor widget
                ed = QKeySequenceEdit(self)
                seq_str = spec.shortcut or spec.default or ""
                if seq_str:
                    with silent_operation("set_key_sequence"):
                        ed.setKeySequence(QKeySequence(seq_str))
                # connect editingFinished to handler
                ed.editingFinished.connect(lambda cid=spec.cid, w=ed: self._on_seq_changed(cid, w))
                self.tree.setItemWidget(child, 1, ed)
                self._edits[spec.cid] = ed

        # Allow keyboard navigation and editing
        self.tree.setFocus()

        # Status footer for conflicts
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #aa0000;")
        layout.addWidget(self._status)

        # Buttons
        btns = QHBoxLayout()
        reset_all = QPushButton("Reset All to Defaults")
        reset_all.clicked.connect(self._reset_all)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(reset_all)
        btns.addWidget(close_btn)
        layout.addLayout(btns)
        with silent_operation("init_conflict_styles"):
            # Update conflict highlighting initially and when manager mapping changes
            self._refresh_conflict_styles()
            self.manager.keybinds_changed.connect(self._refresh_conflict_styles)

    def _on_seq_changed(self, cid: str, widget: 'QKeySequenceEdit') -> None:
        seq = widget.keySequence()
        seq_str = seq.toString() if not seq.isEmpty() else ""
        self.manager.set_shortcut(cid, seq_str or None)
        self._refresh_conflict_styles()

    def _reset_all(self) -> None:
        # Remove custom shortcuts so defaults apply
        for spec in self.manager.list_commands():
            self.manager.reset_shortcut(spec.cid)
        # Refresh UI: update all editors
        for cid, ed in self._edits.items():
            spec = next((s for s in self.manager.list_commands() if s.cid == cid), None)
            if spec:
                seq_str = spec.default or ""
                ed.setKeySequence(QKeySequence(seq_str))
        self._refresh_conflict_styles()

    def _refresh_conflict_styles(self) -> None:
        """Highlight rows whose shortcuts conflict and show a summary message."""
        try:
            conflicts = getattr(self.manager, 'get_conflicts', lambda: {})()
        except Exception:
            conflicts = {}
        # Reset all styles on tree children
        try:
            it = QTreeWidgetItem()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # clear backgrounds and widget styles
        def _clear_styles(node: QTreeWidgetItem):
            with silent_operation("clear_tree_styles"):
                for col in range(self.tree.columnCount()):
                    node.setBackground(col, QBrush())
                w = self.tree.itemWidget(node, 1)
                if w:
                    w.setStyleSheet("")

        # iterate categories and children
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            cat_item = root.child(i)
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                _clear_styles(child)

        if not conflicts:
            if hasattr(self, '_status') and self._status is not None:
                self._status.setText("")
            return

        # Build reverse map cid -> conflict key
        cid_to_conf = {}
        for key, ids in conflicts.items():
            for cid in ids:
                cid_to_conf[cid] = key

        # Apply highlight to children with conflicts: vivid red background, white text,
        # and a black outline around the editor widget for visibility.
        red_brush = QBrush(QColor(200, 0, 0))
        white_brush = QBrush(QColor(255, 255, 255))
        for i in range(root.childCount()):
            cat_item = root.child(i)
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                cid = child.data(0, Qt.ItemDataRole.UserRole)
                if cid in cid_to_conf:
                    try:
                        # Set background and foreground for all columns
                        for col in range(self.tree.columnCount()):
                            child.setBackground(col, red_brush)
                            child.setForeground(col, white_brush)
                        # Style the QKeySequenceEdit (or widget) with white text, red bg and black border
                        w = self.tree.itemWidget(child, 1)
                        if w:
                            try:
                                w.setStyleSheet(
                                    "background-color: #c80000; color: #ffffff; border: 2px solid #000000; font-weight: bold;"
                                )
                            except Exception:
                                # Fallback to simpler style if needed
                                try:
                                    w.setStyleSheet("background-color: #c80000; color: #ffffff;")
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Status message
        try:
            parts = []
            for key, ids in conflicts.items():
                labels = []
                lab_map = {s.cid: s.label for s in self.manager.list_commands()}
                for cid in ids:
                    labels.append(lab_map.get(cid, cid))
                parts.append(f"{key}: {', '.join(labels)}")
            if hasattr(self, '_status') and self._status is not None:
                self._status.setText("Conflicting shortcuts detected (first listed wins):\n" + "\n".join(parts))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class ErrorDialog(EditorDialogBase): # Central error reporting dialog with copy button

    """A dialog to display errors with a copy-pasteable diagnostics section.

    Use show_error(...) helper to open this consistently.
    """
    def __init__(self, title: str, message: str, details: Optional[str] = None, parent: Optional[QWidget] = None, modal: bool = True):
        super().__init__(parent, title=title or "Error", modal=modal)
        layout = QVBoxLayout(self)
        lbl = QLabel(obfuscate_text(message) if message else "An error occurred.")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        if details:
            txt = QPlainTextEdit(obfuscate_text(details))
            txt.setReadOnly(True)
            txt.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            txt.setMinimumHeight(160)
            layout.addWidget(txt)
        btns = QHBoxLayout()
        copy_btn = QPushButton("Copy Details")
        close_btn = QPushButton("Close")
        def _copy():
            with silent_operation("copy_error_to_clipboard"):
                diag = details if details else message
                if diag:
                    QApplication.clipboard().setText(obfuscate_text(diag))
        copy_btn.clicked.connect(_copy)
        close_btn.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(copy_btn)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

class KeybindsOverlay(QWidget):
    """A lightweight, always-on-top overlay that lists currently active keybindings.

    It's non-modal and intended to be toggled with a quick key (e.g. Ctrl+/) and
    will auto-hide after a short timeout unless pinned.
    """
    def __init__(self, manager: 'KeybindsManager', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._list = QListWidget(self)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel('Keybindings (press again to hide)', self))
        layout.addWidget(self._list)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self.hide)
        self._pinned = False

    def show_overlay(self, duration_ms: int = 5000):
        with silent_operation("show_keybinds_overlay"):
            self._list.clear()
            for spec in sorted(self.manager.list_commands(), key=lambda s: (s.category or 'ZZZ', s.label.lower())):
                seq = spec.shortcut or spec.default or ''
                if seq:
                    self._list.addItem(f"{spec.category or 'General'}: {spec.label} — {seq}")
            self.adjustSize()
            self.show()
            self._timeout.start(duration_ms)

    def toggle_overlay(self):
        if self.isVisible():
            self.hide()
        else:
            self.show_overlay()

#endregion

#region Graphics
# Grid and scene configuration
GRID_UNIT = 300.0  # px per grid unit
FOCUS_WIDTH = 260.0
FOCUS_HEIGHT = 140.0
SCENE_HALF = 60000 # Scene half-extent in pixels. Increase significantly to allow larger trees.









# Module-level compatibility alias for older code/tests that import `NoteItem`.
# Placed at module scope so `from _focusGUI import NoteItem` works reliably.
NoteItem = None
with silent_operation("alias_NoteItem"):
    NoteItem = NoteNode






# Import all command classes from _commands module
try:
    from _commands import *
except Exception:
    logger.exception("Failed to import _commands; commands will be unavailable")

# From _nodes module for visual node representations
try:
    from _nodes import (
        ConnectionItem, NodeBase, LShapedConnectionLine, MutualExclusiveConnector,
        FocusNode, EventNode, NoteNode, NoteConnectionLine, NoteFocusConnector,
        NoteEventConnector, EventFocusConnector, EventEventConnector
    )
except Exception:
    logger.exception("Failed to import from _nodes; node classes will be unavailable")

#endregion

#region Canvas
# scene with nodes and connections







class FocusSpatialIndex:
    """Spatial hash index for QGraphicsItems to accelerate viewport queries."""

    def __init__(self, cell_size: float = 300.0):
        self.cell_size = float(cell_size) if cell_size else 300.0
        self._cells: Dict[Tuple[int, int], set] = defaultdict(set)
        self._node_cells: Dict['QGraphicsItem', List[Tuple[int, int]]] = {}

    def _cells_for_rect(self, rect: QRectF) -> List[Tuple[int, int]]:
        if rect.isNull():
            return []
        cs = self.cell_size or 1.0
        left = int(math.floor(rect.left() / cs))
        right = int(math.floor(rect.right() / cs))
        top = int(math.floor(rect.top() / cs))
        bottom = int(math.floor(rect.bottom() / cs))
        cells = []
        for ix in range(left, right + 1):
            for iy in range(top, bottom + 1):
                cells.append((ix, iy))
        return cells

    def insert(self, node: 'QGraphicsItem', rect: QRectF) -> None:
        cells = self._cells_for_rect(rect)
        if not cells:
            self._node_cells[node] = []
            return
        for cell in cells:
            self._cells[cell].add(node)
        self._node_cells[node] = cells

    def update(self, node: 'QGraphicsItem', rect: QRectF) -> None:
        self.remove(node)
        self.insert(node, rect)

    def remove(self, node: 'QGraphicsItem') -> None:
        cells = self._node_cells.pop(node, [])
        for cell in cells:
            bucket = self._cells.get(cell)
            if not bucket:
                continue
            bucket.discard(node)
            if not bucket:
                self._cells.pop(cell, None)

    def query(self, rect: QRectF) -> set:
        result: set = set()
        for cell in self._cells_for_rect(rect):
            bucket = self._cells.get(cell)
            if not bucket:
                continue
            for node in bucket:
                try:
                    nrect = node.sceneBoundingRect()
                except Exception:
                    continue
                if nrect.intersects(rect):
                    result.add(node)
        return result

    def clear(self) -> None:
        self._cells.clear()
        self._node_cells.clear()

class FocusTreeCanvas(QGraphicsScene):
    """Canvas for visual focus tree editing with enhanced features"""
    _render_stack_attr_map: Dict[str, Dict[str, str]] = {
        'node': {'offset_x': 'focus_render_offset_x', 'offset_y': 'focus_render_offset_y'},
        'title': {'offset_x': 'focus_title_offset_x', 'offset_y': 'focus_title_offset_y'},
        'icon': {'offset_x': 'focus_icon_offset_x', 'offset_y': 'focus_icon_offset_y'},
        'pill': {
            'offset_x': 'focus_pill_offset_x',
            'offset_y': 'focus_pill_offset_y',
            'padding': 'title_pill_padding',
            'mode': 'title_pill_mode',
        },
    }
    def __init__(self, parent):
        super().__init__(parent)
        # Keep a direct reference to the owning main window for routing actions
        # Note: do NOT rely solely on QObject.parent() here; code frequently accesses
        # scene.editor. We also keep the legacy `parent` attribute for any existing uses.
        self.parent = parent
        self.editor = parent
        # Z-order layering constants (background < grid < connections < nodes)
        self.z_background = -20
        self.z_grid = -10
        self.z_connections = 0
        self.z_nodes = 10
        self.nodes = {}
        self.connections = []
        self.connection_mode = False
        self.connection_start = None
        # Prerequisite link creation mode: None (normal), 'OR' or 'AND'
        self.prereq_link_mode = None
        # Spatial index + visibility cache for large scenes
        self._spatial_index = FocusSpatialIndex(cell_size=GRID_UNIT)
        self._visible_nodes_cache = weakref.WeakSet()
        # Optional drag-to-link mode: when True, left-click on a node starts a rubber-banded
        # temporary line to the mouse cursor; left-click on a second node completes the link.
        self.drag_to_link_mode = False
        # Temporary rubber-band graphics item (QGraphicsPathItem) used while drag-to-link is active
        self._temp_link_item = None
        self.setSceneRect(-SCENE_HALF, -SCENE_HALF, SCENE_HALF * 2, SCENE_HALF * 2)
        # Grid state
        self._grid_visible = True
        # Draw grid
        self.draw_grid()
        # Palette for network colors (expandable). We'll compute dynamically per-network.
        self.network_colors = {}
        self._palette_seed = None
        # Frame items grouped by network or group id
        self.frames = []
        self.frames_enabled = False
        # Frame type visibility toggles for better control
        self.show_network_frames = True
        self.show_layer_frames = True
        self.show_subtree_frames = True
        self.show_lineage_frames = True
        self.show_frame_labels = True
        # per-network variant counters to produce slightly different shades for multiple frames
        self._frame_variant_counters = {}
        # small timer to throttle frame updates
        self._frame_timer = QTimer()
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(120)  # ms
        self._frame_timer.timeout.connect(self.update_frames)
        # Flag used to suppress intermediate frame updates during transitions
        self._frames_transition_in_progress = False
        # layer rendering state: colors and visibility
        self.layer_colors = {}
        self.layer_visibility = {}
        # cache of QGraphicsRectItems per layer to avoid recreation
        self._layer_frame_items = {}
        # dirty flags to avoid unnecessary recomputation
        self._frames_dirty = True
        self._lineage_dirty = True
        # optimize scene indexing for many dynamic items
        # Use a spatial index (BSP tree) where available so the scene can
        # efficiently query items inside the viewport instead of iterating
        # all items on every paint. Fall back to NoIndex if not supported.
        with silent_operation("set_bsp_index"):
            self.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)

        # Viewport culling: maintain a short-timed cull operation that hides
        # items outside an extended viewport margin to avoid painting and
        # layout work for offscreen nodes on low-end hardware.
        self._cull_timer = QTimer()
        self._cull_timer.setSingleShot(True)
        self._cull_timer.setInterval(150)  # milliseconds; throttle frequency
        self._cull_timer.timeout.connect(self._perform_cull)
        # Margin (pixels) beyond viewport to keep items visible slightly offscreen
        # Users can configure this for massive focus trees (larger = more offscreen items loaded)
        self._cull_margin = 300  # Default: 300 pixels, configurable via settings
        # Culling controls: for small projects, disable viewport culling entirely to
        # avoid unnecessary visibility churn and line flicker.
        self.culling_enabled = True
        self.culling_min_nodes = 150
        self.culling_min_connections = 250

        # Zoom-based culling: hide details when zoomed far out for better performance
        # This complements viewport culling for massive focus trees
        self.zoom_culling_enabled = True  # Enable/disable zoom-based culling
        self.zoom_cull_threshold = 0.3    # Zoom level below which culling becomes aggressive (0.0-1.0)
        # When zoom < threshold, reduce cull margin by this factor (0.0-1.0)
        # 0.0 = disable viewport culling when zoomed out, 1.0 = keep full margin
        self.zoom_cull_margin_factor = 0.4  # At very zoomed out, use 40% of normal margin
        # Minimum pixels to keep visible even at extreme zoom (prevents total unload)
        self.zoom_cull_min_margin = 100

        # lineage highlight and coloring state
        self._lineage_active = False
        self._lineage_ids = set()
        self._lineage_of_node = {}
        self._lineage_colors = {}
        self._precomputed_lineage_rects = {}
        self.color_lines_by_lineage = True
        self.visualizer_lineage_mode = True
        # Visualizer: per-leaf connection lineage coloring
        self._prev_frames_enabled = True
        self._leaf_of_node = {}
        self._leaf_colors = {}
        self._leaf_dirty = True
        # Focus title font size (pt)
        self.focus_title_font_size = 13
        # Event font sizes (pt) - configurable in Appearance -> Country event
        self.event_title_font_size = 14
        self.event_desc_font_size = 10
        # Highlighted app default: increase event options font size for readability
        self.event_options_font_size = 16
        # Connection line width (solid lines)
        self.connection_line_width = 2
        # Connection LOD (Level of Detail) threshold for simplified rendering when zoomed out
        # Set to 0.0 to DISABLE LOD simplification entirely - always render curved lines for maximum fidelity
        # This ensures even massive focus trees render beautifully at any zoom level
        # Users can increase this in settings (0.25, 0.5, etc) if they need performance on low-end hardware
        self.connection_lod_threshold = 0.0  # Fully disabled (was 0.01, original was 0.25)
        # Dynamic title and icon scaling for viewing large focus trees at low zoom
        # When zoomed out, scales up title text and icons to maintain readability/editability
        self.enable_dynamic_title_icon_scaling = True  # Enable/disable dynamic scaling
        self.title_icon_scale_zoom_threshold = 0.3    # Below this zoom level, start scaling (0.3 = 30%)
        self.title_icon_scale_max_multiplier = 2.5    # Maximum scale factor (1.0 = normal, 2.5 = 250%)
        # Simple rendering threshold - below this zoom, use colored rectangles instead of full icons/titles
        # Set to 0.0 to NEVER use simple rendering (always show full details with dynamic scaling)
        # Higher values (0.5, 1.0) use simple rendering more aggressively for performance
        self.simple_render_zoom_threshold = 0.0  # Disabled by default - always show full rendering
        # Track last known zoom for cache invalidation when crossing threshold
        self._last_zoom_scale = 1.0
        self._last_scaling_active = False
        # Icon view mode and related appearance settings
        self.icon_view_mode = True
        # Maximum pixel dimension for icon display in icon-view mode (cap)
        self.icon_view_icon_max = 120
        # Whether to draw a subtle background behind icons in icon-view mode
        self.icon_view_show_background = True
        # Title outline rendering (black outline behind white text)
        self.title_outline_enabled = True
        self.title_outline_thickness = 1
        # Outline color (hex string)
        self.title_outline_color = '#000000'
        # Title pill customization
        self.title_pill_mode = 'image'  # 'default' | 'image' | 'none'
        # Default app-supplied pill image (HOI4 titlebar pill)
        self.title_pill_image_path = 'gfx/interface/focusview/titlebar/focus_can_start_bg.dds'
        self._title_pill_pixmap = None  # cache for image pill
        # padding added around native pill image when expanding pill rect
        self.title_pill_padding = 8.0
        # Prefer Pillow for loading TGA/DDS when available
        self.prefer_pillow_tga = True
        # Enable country event rendering by default (Appearance -> Country Event Rendering)
        self.country_event_mode = True
        # Default SSAA scale for country event background rendering
        self.country_event_ssaa = 1.0
        # Render Stack defaults: per-item X/Y offsets shown in Appearance -> Render Stack Positioning
        self.focus_title_offset_x = 0
        self.focus_title_offset_y = 0
        self.focus_icon_offset_x = 0
        self.focus_icon_offset_y = 0
        self.focus_pill_offset_x = 1
        self.focus_pill_offset_y = -13
        self.event_title_offset_x = 0
        # Slightly lower title to match highlighted app default
        self.event_title_offset_y = 13
        self.event_desc_offset_x = 25
        self.event_desc_offset_y = 0
        self.event_options_offset_x = -100
        # Adjusted Y offset per highlighted app defaults (app default setting)
        self.event_options_offset_y = -53
        # Show node IDs by default in Title & Pill settings
        self.render_node_ids = True
        # Icon quality (supersampling factor for crisper downscaled icons)
        self.icon_supersample_scale = 1.0  # 1.0 = off; e.g., 2.0 for 2x SSAA
        # Mutex icon rendering settings (display scale and supersample factor)
        self.mutex_icon_supersample_scale = 1.0
        self.mutex_icon_display_scale = 1.0
        # Focus color overrides (per focus id) and default color for visibility
        self.focus_color_overrides = {}
        # Utility: safe remove item helper will be defined at class level below
        self.default_focus_color = None
        # Note connection curve strength (per-canvas multiplier; 0.0 = straight, 1.0 = default)
        self.note_connection_curve_strength = 1.0
        # throttle for automatic reflow of isolated nodes
        self._reflow_timer = QTimer()
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(120)
        self._reflow_timer.timeout.connect(self.reflow_unconnected_nodes)
        # Global toggle to allow/disallow automatic layout adjustments.
        # Default False to avoid unintended repositioning.
        self.auto_layout_enabled = False
        # Reflow guard to avoid re-entrant / rapid repeated layout runs
        self._layout_in_progress = False
        # floating notes
        self.notes_enabled = False
        self._notes_items = []
        # project-level note defaults and connections
        self.note_defaults = {
            'title_size': 11,
            'body_size': 11,
            'title_color': '#141414',
            'text_color': '#1E1E1E',
            'bg_color': '#FFF7D2',
            'connection_color': '#5A5A5A',
            'connection_width': 2,
        }
        self._note_connections = []
        # note→focus connectors storage
        self._note_focus_links = []
        # note→event connectors storage
        self._note_event_links = []
        # generic cross-type links: event↔focus and event↔event
        self._event_focus_links = []
        self._event_event_links = []
        # event nodes mapping by id
        self.event_nodes = {}
        # quick lookup for notes by id
        self._notes_by_id = {}
        # mutual exclusivity connectors (keyed by sorted pair of ids)
        self.mutex_connectors = {}
        # Render stack overrides (per-element attribute adjustments)
        self._render_stack_overrides: Dict[str, Dict[str, Any]] = {}

        # Hidden branch index: map tag->list of FocusNode for toggling visibility
        self._hidden_tag_index = {}
        # Global default for whether hidden branches are shown; toggles will modify this
        self._show_hidden_branches_by_tag = {}

        # Autosave / mute defaults
        # Single source of truth for preferences
        try:
            self._preferences = {
                'prefer_app_settings': False,
                'muted': False,
                'autosave_enabled': False,
                'autosave_interval_min': 5,
                'autosave_overwrite': True,
                'autosave_rotate': False,
                'autosave_rotate_count': 6,
            }
        except Exception:
            self._preferences = {}
        # reflect into attributes for backward compatibility
        try:
            self.prefer_app_settings = bool(self._preferences.get('prefer_app_settings', False))
            self.muted = bool(self._preferences.get('muted', False))
            self.autosave_enabled = bool(self._preferences.get('autosave_enabled', False))
            self.autosave_interval_min = int(self._preferences.get('autosave_interval_min', 5) or 5)
            self.autosave_overwrite = bool(self._preferences.get('autosave_overwrite', True))
            self.autosave_rotate = bool(self._preferences.get('autosave_rotate', False))
            self.autosave_rotate_count = int(self._preferences.get('autosave_rotate_count', 6) or 6)
        except Exception:
            # fallback defaults
            self.prefer_app_settings = False
            self.muted = False
            self.autosave_enabled = False
            self.autosave_interval_min = 5
            self.autosave_overwrite = True
            self.autosave_rotate = False
            self.autosave_rotate_count = 6
        self._autosave_timer = None
        self._autosave_in_progress = False
        self._last_autosave_path = None

    def set_render_stack_override(self, element: str, **attrs: Any) -> None:
        """Set per-element overrides for render stack drawing parameters."""
        if not element:
            return
        existing = self._render_stack_overrides.setdefault(str(element), {})
        existing.update({k: v for k, v in attrs.items() if v is not None})

    def clear_render_stack_override(self, element: Optional[str] = None) -> None:
        """Clear render stack overrides (all or per element)."""
        if element is None:
            self._render_stack_overrides.clear()
        else:
            self._render_stack_overrides.pop(str(element), None)

    def get_render_stack_value(self, element: str, key: str, default: Any = None) -> Any:
        """Resolve a render-stack attribute considering overrides and canvas defaults."""
        if not element or not key:
            return default
        override = self._render_stack_overrides.get(str(element), {})
        if key in override:
            return override[key]
        mapping = self._render_stack_attr_map.get(str(element))
        if mapping:
            attr_name = mapping.get(key)
            if attr_name and hasattr(self, attr_name):
                return getattr(self, attr_name)
        return default

    def refresh_mutex_connectors(self) -> None:
        """Create/update/remove visual connectors for mutually exclusive focuses.

        Ensures one connector per unordered pair (A,B) where B is in A.mutually_exclusive
        and both nodes exist on the canvas. Removes stale connectors when exclusivity changes
        or nodes are deleted.
        """
        try:
            # Desired set of pairs from current data. Previously we expanded
            # mutual-exclusion across linked clusters; that created surprising
            # propagation to connected nodes. Only honor explicit
            # `mutually_exclusive` entries on each Focus: create connectors for
            # the explicit pairs listed (A excludes B) and their reciprocal
            # representation will be ensured elsewhere via syncing code.
            desired: Dict[Tuple[str, str], Tuple['FocusNode', 'FocusNode']] = {}

            for a_id, a_node in list(self.nodes.items()):
                mx_list = getattr(a_node.focus, 'mutually_exclusive', []) or []
                for b_id in mx_list:
                    if b_id == a_id:
                        continue
                    b_node = self.nodes.get(b_id)
                    if not b_node:
                        continue
                    key = tuple(sorted((a_id, b_id)))
                    desired[key] = (a_node, b_node)

            # Remove stale connectors
            for key, conn in list(self.mutex_connectors.items()):
                if key not in desired:
                    try:
                        # detach from nodes
                        a_id, b_id = key
                        a_node = self.nodes.get(a_id)
                        b_node = self.nodes.get(b_id)
                        if a_node and conn in getattr(a_node, 'mutex_connectors', []):
                            a_node.mutex_connectors.remove(conn)
                        if b_node and conn in getattr(b_node, 'mutex_connectors', []):
                            b_node.mutex_connectors.remove(conn)
                        self.removeItem(conn)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    del self.mutex_connectors[key]

            # Create missing connectors and update existing ones
            for key, (a_node, b_node) in desired.items():
                conn = self.mutex_connectors.get(key)
                if conn is None:
                    conn = MutualExclusiveConnector(a_node, b_node)
                    self.addItem(conn)
                    self.mutex_connectors[key] = conn
                    # attach to nodes for update-on-move
                    try:
                        if conn not in a_node.mutex_connectors:
                            a_node.mutex_connectors.append(conn)
                        if conn not in b_node.mutex_connectors:
                            b_node.mutex_connectors.append(conn)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        pass
                else:
                    try:
                        conn.update_path()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def to_settings(self) -> Dict[str, Any]:
        """Serialize canvas-related visual settings to a JSON-safe dict."""
        try:
            foc_over = {k: v.name() if isinstance(v, QColor) else str(v) for k, v in self.focus_color_overrides.items()}
        except Exception:
            foc_over = {}
        return {
            'frames_enabled': bool(self.frames_enabled),
            'visualizer_lineage_mode': bool(self.visualizer_lineage_mode),
            'color_lines_by_lineage': bool(self.color_lines_by_lineage),
            'auto_layout_enabled': bool(getattr(self, 'auto_layout_enabled', False)),
            'icon_view_mode': bool(getattr(self, 'icon_view_mode', False)),
            'icon_view_icon_max': int(getattr(self, 'icon_view_icon_max', 120)),
            'icon_view_show_background': bool(getattr(self, 'icon_view_show_background', False)),
            'title_outline_enabled': bool(getattr(self, 'title_outline_enabled', True)),
            'title_outline_thickness': int(getattr(self, 'title_outline_thickness', 1)),
            'title_pill_mode': str(getattr(self, 'title_pill_mode', 'default')),
            'title_pill_image_path': str(getattr(self, 'title_pill_image_path', '') or ''),
            'title_pill_padding': float(getattr(self, 'title_pill_padding', 8.0)),
            'note_defaults': getattr(self, 'note_defaults', {}),
            'note_connection_curve_strength': float(getattr(self, 'note_connection_curve_strength', 1.0)),
            'title_outline_color': str(getattr(self, 'title_outline_color', '#000000') or '#000000'),
            'prefer_pillow_tga': bool(getattr(self, 'prefer_pillow_tga', True)),
            'grid_visible': bool(getattr(self, '_grid_visible', True)),
            'focus_title_font_size': int(getattr(self, 'focus_title_font_size', 14)),
            'connection_line_width': int(getattr(self, 'connection_line_width', 2)),
            'connection_lod_threshold': float(getattr(self, 'connection_lod_threshold', 0.0)),
            'enable_dynamic_title_icon_scaling': bool(getattr(self, 'enable_dynamic_title_icon_scaling', True)),
            'title_icon_scale_zoom_threshold': float(getattr(self, 'title_icon_scale_zoom_threshold', 0.3)),
            'title_icon_scale_max_multiplier': float(getattr(self, 'title_icon_scale_max_multiplier', 2.5)),
            'simple_render_zoom_threshold': float(getattr(self, 'simple_render_zoom_threshold', 0.0)),
            'icon_supersample_scale': float(getattr(self, 'icon_supersample_scale', 1.0)),
            'mutex_icon_supersample_scale': float(getattr(self, 'mutex_icon_supersample_scale', 1.0)),
            'mutex_icon_display_scale': float(getattr(self, 'mutex_icon_display_scale', 1.0)),
            'default_focus_color': self.default_focus_color.name() if isinstance(self.default_focus_color, QColor) else None,
            'undo_limit': int(getattr(self, 'undo_limit', 100)),
            'focus_color_overrides': foc_over,
            'notes_enabled': bool(getattr(self, 'notes_enabled', False)),
            # Render stack positioning offsets
            'focus_render_offset_x': int(getattr(self, 'focus_render_offset_x', 0)),
            'focus_render_offset_y': int(getattr(self, 'focus_render_offset_y', 0)),
            'focus_title_offset_x': int(getattr(self, 'focus_title_offset_x', 0)),
            'focus_title_offset_y': int(getattr(self, 'focus_title_offset_y', 0)),
            'focus_icon_offset_x': int(getattr(self, 'focus_icon_offset_x', 0)),
            'focus_icon_offset_y': int(getattr(self, 'focus_icon_offset_y', 0)),
            'focus_pill_offset_x': int(getattr(self, 'focus_pill_offset_x', 0)),
            'focus_pill_offset_y': int(getattr(self, 'focus_pill_offset_y', 0)),
            'event_title_offset_x': int(getattr(self, 'event_title_offset_x', 0)),
            'event_title_offset_y': int(getattr(self, 'event_title_offset_y', 0)),
            'event_desc_offset_x': int(getattr(self, 'event_desc_offset_x', 0)),
            'event_desc_offset_y': int(getattr(self, 'event_desc_offset_y', 0)),
            'event_options_offset_x': int(getattr(self, 'event_options_offset_x', 0)),
            'event_options_offset_y': int(getattr(self, 'event_options_offset_y', 0)),
            # Event font sizes
            'event_title_font_size': int(getattr(self, 'event_title_font_size', 14)),
            'event_desc_font_size': int(getattr(self, 'event_desc_font_size', 10)),
            'event_options_font_size': int(getattr(self, 'event_options_font_size', 10)),
                'drag_to_link_mode': bool(getattr(self, 'drag_to_link_mode', False)),
            # Country event rendering options
            'country_event_mode': bool(getattr(self, 'country_event_mode', False)),
            'country_event_ssaa': float(getattr(self, 'country_event_ssaa', getattr(self, 'event_supersample_scale', 1.0)) or 1.0),
            'country_event_bg_path': str(getattr(self, 'country_event_bg_path', '') or getattr(self, 'country_event_bg', '') or ''),
            'country_event_overlay_path': str(getattr(self, 'country_event_overlay_path', '') or getattr(self, 'country_event_overlay', '') or ''),
            'country_event_title_offset': float(getattr(self, 'country_event_title_offset', 10.0)),
            # Render node ids toggle
            'render_node_ids': bool(getattr(self, 'render_node_ids', True)),
            # Performance: focus loading distance (cull margin)
            'cull_margin': int(getattr(self, '_cull_margin', 300)),
            # Performance: zoom-based culling settings
            'zoom_culling_enabled': bool(getattr(self, 'zoom_culling_enabled', True)),
            'zoom_cull_threshold': float(getattr(self, 'zoom_cull_threshold', 0.3)),
            'zoom_cull_margin_factor': float(getattr(self, 'zoom_cull_margin_factor', 0.4)),
            'zoom_cull_min_margin': int(getattr(self, 'zoom_cull_min_margin', 100)),
        }
    def apply_settings(self, data: Dict[str, Any]) -> None:
        """Apply settings from dict and refresh visuals."""
        try:
            self.frames_enabled = bool(data.get('frames_enabled', self.frames_enabled))
            self.visualizer_lineage_mode = bool(data.get('visualizer_lineage_mode', self.visualizer_lineage_mode))
            self.color_lines_by_lineage = bool(data.get('color_lines_by_lineage', self.color_lines_by_lineage))
            try:
                self.auto_layout_enabled = bool(data.get('auto_layout_enabled', getattr(self, 'auto_layout_enabled', False)))
            except Exception:
                self.auto_layout_enabled = False
            self.icon_view_mode = bool(data.get('icon_view_mode', getattr(self, 'icon_view_mode', False)))
            self.icon_view_icon_max = int(data.get('icon_view_icon_max', getattr(self, 'icon_view_icon_max', 120)))
            self.icon_view_show_background = bool(data.get('icon_view_show_background', getattr(self, 'icon_view_show_background', False)))
            self.title_outline_enabled = bool(data.get('title_outline_enabled', getattr(self, 'title_outline_enabled', True)))
            try:
                self.title_outline_thickness = int(data.get('title_outline_thickness', getattr(self, 'title_outline_thickness', 1)))
            except Exception:
                self.title_outline_thickness = 1
            try:
                toc = data.get('title_outline_color', getattr(self, 'title_outline_color', '#000000'))
                if toc:
                    self.title_outline_color = str(toc)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Title pill customization
            self.title_pill_mode = str(data.get('title_pill_mode', getattr(self, 'title_pill_mode', 'default')) or 'default')
            new_pill_path = str(data.get('title_pill_image_path', getattr(self, 'title_pill_image_path', '') or '') or '')
            # Country event rendering options
            try:
                self.country_event_mode = bool(data.get('country_event_mode', getattr(self, 'country_event_mode', False)))
            except Exception:
                self.country_event_mode = False
            try:
                self.country_event_ssaa = float(data.get('country_event_ssaa', getattr(self, 'country_event_ssaa', getattr(self, 'event_supersample_scale', 1.0))))
            except Exception:
                self.country_event_ssaa = float(getattr(self, 'event_supersample_scale', 1.0))
            try:
                cand_bg = data.get('country_event_bg_path', data.get('country_event_bg', getattr(self, 'country_event_bg_path', '') or getattr(self, 'country_event_bg', '')))
                if cand_bg:
                    self.country_event_bg_path = str(cand_bg)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                cand_ov = data.get('country_event_overlay_path', data.get('country_event_overlay', getattr(self, 'country_event_overlay_path', '') or getattr(self, 'country_event_overlay', '')))
                if cand_ov:
                    self.country_event_overlay_path = str(cand_ov)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                # title offset for country event title (vertical pixels)
                self.country_event_title_offset = float(data.get('country_event_title_offset', getattr(self, 'country_event_title_offset', 10.0)))
            except Exception:
                self.country_event_title_offset = 10.0
            try:
                # Render node ids toggle (default True)
                self.render_node_ids = bool(data.get('render_node_ids', getattr(self, 'render_node_ids', True)))
            except Exception:
                self.render_node_ids = True
            if new_pill_path != getattr(self, 'title_pill_image_path', ''):
                self.title_pill_image_path = new_pill_path
                self._title_pill_pixmap = None
            try:
                self.title_pill_padding = float(data.get('title_pill_padding', getattr(self, 'title_pill_padding', 8.0)))
                try:
                    self.note_connection_curve_strength = float(data.get('note_connection_curve_strength', getattr(self, 'note_connection_curve_strength', 1.0)))
                except Exception:
                    self.note_connection_curve_strength = getattr(self, 'note_connection_curve_strength', 1.0)
            except Exception:
                self.title_pill_padding = 8.0
            try:
                self.undo_limit = int(data.get('undo_limit', getattr(self, 'undo_limit', 100)))
            except Exception:
                self.undo_limit = getattr(self, 'undo_limit', 100)
            self.prefer_pillow_tga = bool(data.get('prefer_pillow_tga', getattr(self, 'prefer_pillow_tga', True)))
            self.focus_title_font_size = int(data.get('focus_title_font_size', self.focus_title_font_size))
            try:
                self.connection_line_width = int(data.get('connection_line_width', getattr(self, 'connection_line_width', 2)))
            except Exception:
                self.connection_line_width = 2
            try:
                self.connection_lod_threshold = float(data.get('connection_lod_threshold', getattr(self, 'connection_lod_threshold', 0.0)))
            except Exception:
                self.connection_lod_threshold = 0.0
            try:
                self.enable_dynamic_title_icon_scaling = bool(data.get('enable_dynamic_title_icon_scaling', getattr(self, 'enable_dynamic_title_icon_scaling', True)))
            except Exception:
                self.enable_dynamic_title_icon_scaling = True
            try:
                self.title_icon_scale_zoom_threshold = float(data.get('title_icon_scale_zoom_threshold', getattr(self, 'title_icon_scale_zoom_threshold', 0.3)))
            except Exception:
                self.title_icon_scale_zoom_threshold = 0.3
            try:
                self.title_icon_scale_max_multiplier = float(data.get('title_icon_scale_max_multiplier', getattr(self, 'title_icon_scale_max_multiplier', 2.5)))
            except Exception:
                self.title_icon_scale_max_multiplier = 2.5
            try:
                self.simple_render_zoom_threshold = float(data.get('simple_render_zoom_threshold', getattr(self, 'simple_render_zoom_threshold', 0.0)))
            except Exception:
                self.simple_render_zoom_threshold = 0.0
            try:
                self.icon_supersample_scale = float(data.get('icon_supersample_scale', getattr(self, 'icon_supersample_scale', 1.0)))
            except Exception:
                self.icon_supersample_scale = 1.0
            # Render stack offsets
            try:
                self.focus_render_offset_x = int(data.get('focus_render_offset_x', getattr(self, 'focus_render_offset_x', 0)))
                self.focus_render_offset_y = int(data.get('focus_render_offset_y', getattr(self, 'focus_render_offset_y', 0)))
                self.focus_title_offset_x = int(data.get('focus_title_offset_x', getattr(self, 'focus_title_offset_x', 0)))
                self.focus_title_offset_y = int(data.get('focus_title_offset_y', getattr(self, 'focus_title_offset_y', 0)))
                self.focus_icon_offset_x = int(data.get('focus_icon_offset_x', getattr(self, 'focus_icon_offset_x', 0)))
                self.focus_icon_offset_y = int(data.get('focus_icon_offset_y', getattr(self, 'focus_icon_offset_y', 0)))
                self.focus_pill_offset_x = int(data.get('focus_pill_offset_x', getattr(self, 'focus_pill_offset_x', 0)))
                self.focus_pill_offset_y = int(data.get('focus_pill_offset_y', getattr(self, 'focus_pill_offset_y', 0)))
                self.event_title_offset_x = int(data.get('event_title_offset_x', getattr(self, 'event_title_offset_x', 0)))
                self.event_title_offset_y = int(data.get('event_title_offset_y', getattr(self, 'event_title_offset_y', 0)))
                self.event_desc_offset_x = int(data.get('event_desc_offset_x', getattr(self, 'event_desc_offset_x', 0)))
                self.event_desc_offset_y = int(data.get('event_desc_offset_y', getattr(self, 'event_desc_offset_y', 0)))
                self.event_options_offset_x = int(data.get('event_options_offset_x', getattr(self, 'event_options_offset_x', 0)))
                self.event_options_offset_y = int(data.get('event_options_offset_y', getattr(self, 'event_options_offset_y', 0)))
                # Event font sizes (pt)
                try:
                    self.event_title_font_size = int(data.get('event_title_font_size', getattr(self, 'event_title_font_size', 14)))
                except Exception:
                    self.event_title_font_size = getattr(self, 'event_title_font_size', 14)
                try:
                    self.event_desc_font_size = int(data.get('event_desc_font_size', getattr(self, 'event_desc_font_size', 10)))
                except Exception:
                    self.event_desc_font_size = getattr(self, 'event_desc_font_size', 10)
                try:
                    self.event_options_font_size = int(data.get('event_options_font_size', getattr(self, 'event_options_font_size', 10)))
                except Exception:
                    self.event_options_font_size = getattr(self, 'event_options_font_size', 10)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.mutex_icon_supersample_scale = float(data.get('mutex_icon_supersample_scale', getattr(self, 'mutex_icon_supersample_scale', 1.0)))
            except Exception:
                self.mutex_icon_supersample_scale = 1.0
            try:
                self.mutex_icon_display_scale = float(data.get('mutex_icon_display_scale', getattr(self, 'mutex_icon_display_scale', 1.0)))
            except Exception:
                self.mutex_icon_display_scale = 1.0
            gv = data.get('grid_visible', getattr(self, '_grid_visible', True))
            self.set_grid_visible(bool(gv))
            # restore note defaults if provided
            try:
                nd = data.get('note_defaults')
                if isinstance(nd, dict):
                    # merge provided values into existing defaults to preserve missing keys
                    cur = getattr(self, 'note_defaults', {}) or {}
                    cur.update({k: v for k, v in nd.items() if v is not None})
                    self.note_defaults = cur
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            dcol = data.get('default_focus_color')
            if dcol:
                try:
                    self.default_focus_color = QColor(str(dcol))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            focs = data.get('focus_color_overrides', {})
            if isinstance(focs, dict):
                self.focus_color_overrides.clear()
                for k, v in focs.items():
                    try:
                        self.focus_color_overrides[str(k)] = QColor(str(v))
                    except Exception:
                        continue
            # notes visibility toggle only; notes are loaded/saved per project
            self.notes_enabled = bool(data.get('notes_enabled', getattr(self, 'notes_enabled', False)))
            try:
                self.drag_to_link_mode = bool(data.get('drag_to_link_mode', getattr(self, 'drag_to_link_mode', False)))
            except Exception:
                self.drag_to_link_mode = False
            # Performance: focus loading distance (cull margin)
            try:
                self._cull_margin = int(data.get('cull_margin', getattr(self, '_cull_margin', 300)))
            except Exception:
                self._cull_margin = 300
            # Performance: zoom-based culling settings
            try:
                self.zoom_culling_enabled = bool(data.get('zoom_culling_enabled', getattr(self, 'zoom_culling_enabled', True)))
                self.zoom_cull_threshold = float(data.get('zoom_cull_threshold', getattr(self, 'zoom_cull_threshold', 0.3)))
                self.zoom_cull_margin_factor = float(data.get('zoom_cull_margin_factor', getattr(self, 'zoom_cull_margin_factor', 0.4)))
                self.zoom_cull_min_margin = int(data.get('zoom_cull_min_margin', getattr(self, 'zoom_cull_min_margin', 100)))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # propagate visibility to notes and note→focus connectors
            try:
                for it in list(getattr(self, '_notes_items', [])):
                    it.set_visible(self.notes_enabled)
                for nf in list(getattr(self, '_note_focus_links', [])):
                    nf.setVisible(self.notes_enabled)
                # Note↔Event links follow notes visibility
                for ne in list(getattr(self, '_note_event_links', [])):
                    ne.setVisible(self.notes_enabled)
                # Event↔Focus links are independent of notes visibility; ensure they stay visible
                for ef in list(getattr(self, '_event_focus_links', [])):
                    ef.setVisible(True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Refresh visuals
            self._frames_dirty = True
            self.schedule_frame_update()
            self.refresh_connection_colors()
            self.update()
            # also refresh mutual exclusivity connectors after settings load
            try:
                self.refresh_mutex_connectors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Force nodes and connections to refresh so any new offsets are applied visually
            try:
                for n in list(getattr(self, 'nodes', {}).values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for c in list(getattr(self, 'connections', [])):
                    try:
                        if hasattr(c, 'update_path'):
                            c.update_path()
                        else:
                            c.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.schedule_frame_update()
                except Exception:
                    try:
                        self.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def draw_grid(self):
        """Draw a grid background"""
        # Remove existing grid lines (if any) to avoid duplication
        for item in list(self.items()):
            # preserve FocusNode and connection lines only; remove simple QGraphicsLineItem grid
            if isinstance(item, QGraphicsLineItem):
                self.removeItem(item)

        if not getattr(self, '_grid_visible', True):
            return

        pen = QPen(QColor(200, 200, 200), 0.5, Qt.PenStyle.DotLine)
        major_pen = QPen(QColor(150, 150, 150), 1, Qt.PenStyle.DotLine)
        # Minor grid lines (every GRID_UNIT) and major grid lines every few units
        step = int(GRID_UNIT)
        major_step = step * 5
        left = int(-SCENE_HALF)
        right = int(SCENE_HALF)
        for x in range(left, right + 1, step):
            line_pen = major_pen if (x - left) % major_step == 0 else pen
            li = self.addLine(x, -SCENE_HALF, x, SCENE_HALF, line_pen)
            with silent_operation("set_grid_line_zvalue"):
                li.setZValue(self.z_grid)
        for y in range(left, right + 1, step):
            line_pen = major_pen if (y - left) % major_step == 0 else pen
            li = self.addLine(-SCENE_HALF, y, SCENE_HALF, y, line_pen)
            with silent_operation("set_grid_line_zvalue"):
                li.setZValue(self.z_grid)

    def schedule_cull(self, delay_ms: Optional[int] = None) -> None:
        """Schedule a cull pass shortly (throttled).

        delay_ms: optional custom delay in milliseconds.
        """
        with silent_operation("schedule_cull"):
            # Skip culling for small scenes or when disabled
            if not getattr(self, 'culling_enabled', True):
                return
            with silent_operation("check_cull_thresholds"):
                if len(getattr(self, 'nodes', {}) or {}) < getattr(self, 'culling_min_nodes', 0) and \
                   len(getattr(self, 'connections', []) or []) < getattr(self, 'culling_min_connections', 0):
                    return
            if delay_ms is not None:
                with silent_operation("set_cull_interval"):
                    self._cull_timer.setInterval(int(delay_ms))
            if self._cull_timer.isActive():
                self._cull_timer.stop()
            self._cull_timer.start()

    def _check_and_invalidate_scaling_cache(self) -> None:
        """Force node updates when zoom changes and dynamic scaling is active.

        Since nodes use NoCache mode for dynamic scaling, we just need to trigger
        updates to force fresh rendering with the new scale factor.
        """
        with silent_operation("check_scaling_cache"):
            # Only check if dynamic scaling is enabled
            if not bool(getattr(self, 'enable_dynamic_title_icon_scaling', True)):
                return

            # Get current zoom scale
            views = self.views()
            if not views:
                return

            current_zoom = views[0].transform().m11()

            last_zoom = float(getattr(self, '_last_zoom_scale', 1.0))

            # Store current zoom for next check
            self._last_zoom_scale = current_zoom

            # If zoom changed by any amount, trigger updates for all visible nodes
            if abs(current_zoom - last_zoom) > 0.001:
                # Trigger viewport update to force fresh render with new scale
                with silent_operation("update_viewport"):
                    views[0].viewport().update()

    def _perform_cull(self) -> None:
        """Perform viewport culling: hide/show nodes and connections based on
        intersection with the current view's extended scene rect.

        This reduces painting and layout work for offscreen items.
        """
        try:
            # Check if zoom crossed the dynamic scaling threshold and invalidate caches if needed
            try:
                self._check_and_invalidate_scaling_cache()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Skip when disabled or when scene is small enough to keep all visible
            if not getattr(self, 'culling_enabled', True):
                return
                return
            try:
                if len(getattr(self, 'nodes', {}) or {}) < getattr(self, 'culling_min_nodes', 0) and \
                   len(getattr(self, 'connections', []) or []) < getattr(self, 'culling_min_connections', 0):
                    return
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            views = self.views()
            if not views:
                return
            # Use the first attached view for culling decisions
            view = views[0]
            try:
                vp_rect = view.viewport().rect()
                scene_rect = view.mapToScene(vp_rect).boundingRect()
            except Exception:
                scene_rect = view.mapToScene(view.rect()).boundingRect()

            # Calculate effective margin with zoom-based adjustment
            mr = float(getattr(self, '_cull_margin', 300))

            # Apply zoom-based culling if enabled
            if getattr(self, 'zoom_culling_enabled', True):
                try:
                    # Get current zoom level (scale factor)
                    zoom_threshold = float(getattr(self, 'zoom_cull_threshold', 0.3))
                    zoom_level = view.transform().m11()  # horizontal scale component

                    # If zoomed out below threshold, reduce margin for better performance
                    if zoom_level < zoom_threshold:
                        margin_factor = float(getattr(self, 'zoom_cull_margin_factor', 0.4))
                        min_margin = float(getattr(self, 'zoom_cull_min_margin', 100))
                        # Linearly interpolate between full margin at threshold and reduced at 0
                        scale_ratio = zoom_level / zoom_threshold if zoom_threshold > 0 else 0
                        reduced_margin = mr * margin_factor
                        mr = max(min_margin, reduced_margin + (mr - reduced_margin) * scale_ratio)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # expand by calculated margin
            ext = QRectF(scene_rect.adjusted(-mr, -mr, mr, mr))

            # Determine candidate nodes by querying the spatial index; fall back to all nodes
            if hasattr(self, '_spatial_index'):
                try:
                    candidate_nodes = set(self._spatial_index.query(ext))
                except Exception:
                    candidate_nodes = set(self.nodes.values())
            else:
                candidate_nodes = set(self.nodes.values())

            active_visible = set()
            for node in candidate_nodes:
                try:
                    nb = node.sceneBoundingRect()
                except Exception:
                    continue
                vis = nb.intersects(ext)
                # Respect hidden branch tags
                try:
                    fobj = getattr(node, 'focus', None)
                    if fobj is not None and getattr(fobj, 'hidden', False):
                        tags = list(getattr(fobj, 'hidden_tags', []) or [])
                        allow = False
                        for t in tags:
                            try:
                                if self._show_hidden_branches_by_tag.get(t, False):
                                    allow = True
                                    break
                            except Exception:
                                continue
                        if not allow:
                            vis = False
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                try:
                    current_visible = node.isVisible()
                except Exception:
                    current_visible = vis
                if current_visible != vis:
                    try:
                        node.set_logical_visible(bool(vis), user=False)
                    except Exception:
                        node.setVisible(vis)
                if vis:
                    active_visible.add(node)

            # Hide any nodes that were previously visible but are no longer within the frustum
            cache = getattr(self, '_visible_nodes_cache', None)
            if cache is not None:
                try:
                    for node in list(cache):
                        if node not in active_visible:
                            try:
                                if node.isVisible():
                                    try:
                                        node.set_logical_visible(False, user=False)
                                    except Exception:
                                        node.setVisible(False)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            cache.discard(node)
                    for node in active_visible:
                        cache.add(node)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Cull connections similarly (keep if either endpoint visible)
            for conn in list(self.connections):
                try:
                    # Some connection items may be custom with start/end nodes
                    start_node = getattr(conn, 'start_node', None)
                    end_node = getattr(conn, 'end_node', None)
                    if start_node is not None and end_node is not None:
                        # If either endpoint is a hidden focus (and its tags are not enabled), the connection should be hidden
                        try:
                            def _node_allowed(n):
                                try:
                                    fobj = getattr(n, 'focus', None)
                                    if fobj is not None and getattr(fobj, 'hidden', False):
                                        tags = list(getattr(fobj, 'hidden_tags', []) or [])
                                        for t in tags:
                                            try:
                                                if self._show_hidden_branches_by_tag.get(t, False):
                                                    return True
                                            except Exception:
                                                continue
                                        return False
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                return True
                            if not _node_allowed(start_node) or not _node_allowed(end_node):
                                vis = False
                            else:
                                vis = (start_node.isVisible() and end_node.isVisible()) or start_node.sceneBoundingRect().intersects(ext) or end_node.sceneBoundingRect().intersects(ext)
                        except Exception:
                            vis = (start_node.isVisible() and end_node.isVisible()) or start_node.sceneBoundingRect().intersects(ext) or end_node.sceneBoundingRect().intersects(ext)
                    else:
                        vis = conn.sceneBoundingRect().intersects(ext)
                    if conn.isVisible() != vis:
                        conn.setVisible(vis)
                except Exception:
                    continue
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def safe_remove_item(self, item: QGraphicsItem) -> None:
        """Remove `item` from this scene only if it's actually in the same scene.

        This avoids the QGraphicsScene::removeItem scene-mismatch error when
        code attempts to remove an item that belongs to another scene or has
        already been removed (item.scene() is None).
        """
        try:
            if item is None:
                return
            # item.scene() can be None if already removed
            try:
                its_scene = item.scene()
            except Exception:
                its_scene = None
            if its_scene is self:
                try:
                    super(FocusTreeCanvas, self).removeItem(item)
                except Exception:
                    # fallback to QGraphicsScene.removeItem
                    try:
                        self.removeItem(item)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # Floating notes helpers
    def _create_note_item(self, text: str, pos: QPointF) -> 'NoteNode':
        d = getattr(self, 'note_defaults', {})
        note = NoteNode(text, pos,
                        title_size=d.get('title_size'), body_size=d.get('body_size'),
                        title_color=QColor(d.get('title_color')) if d.get('title_color') else None,
                        text_color=QColor(d.get('text_color')) if d.get('text_color') else None,
                        color=QColor(d.get('bg_color')) if d.get('bg_color') else None)
        self.addItem(note)
        return note

    def add_note(self, text: str = "Note", scene_pos: Optional[QPointF] = None) -> None:
        if scene_pos is None:
            scene_pos = QPointF(0, 0)
        it = self._create_note_item(text, scene_pos)
        it.set_visible(self.notes_enabled)
        self._notes_items.append(it)

    def clear_notes(self) -> None:
        for it in list(self._notes_items):
            try:
                if hasattr(self, 'safe_remove_item'):
                    self.safe_remove_item(it)
                else:
                    try:
                        self.removeItem(it)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self._notes_items = []
        # also clear note connections
        try:
            for ln in list(self._note_connections):
                try:
                    if hasattr(self, 'safe_remove_item'):
                        self.safe_remove_item(ln)
                    else:
                        try:
                            self.removeItem(ln)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self._note_connections.clear()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # also clear note→focus links
        try:
            for nf in list(getattr(self, '_note_focus_links', [])):
                try:
                    if hasattr(self, 'safe_remove_item'):
                        self.safe_remove_item(nf)
                    else:
                        try:
                            self.removeItem(nf)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self._note_focus_links = []
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # Note connections management
    def add_note_connection(self, a: 'NoteNode', b: 'NoteNode') -> Optional['NoteConnectionLine']:
        try:
            if a is None or b is None or a is b:
                return None
            # avoid duplicates by id pair
            aid = getattr(a, 'note_id', None); bid = getattr(b, 'note_id', None)
            if not aid or not bid:
                return None
            pair = tuple(sorted([aid, bid]))
            for ln in list(self._note_connections):
                if tuple(sorted([getattr(ln.a, 'note_id', None), getattr(ln.b, 'note_id', None)])) == pair:
                    return ln
            d = getattr(self, 'note_defaults', {})
            col = QColor(d.get('connection_color')) if d.get('connection_color') else QColor(90, 90, 90)
            width = int(d.get('connection_width', 2))
            ln = NoteConnectionLine(a, b, col, width)
            self.addItem(ln)
            with silent_operation("set_note_connection_zvalue"):
                ln.setZValue(self.z_connections)
            self._note_connections.append(ln)
            with silent_operation("register_note_connection"):
                a._register_note_connection(ln)
                b._register_note_connection(ln)
            return ln
        except Exception:
            return None

    def remove_note_connections_for(self, note: 'NoteNode') -> None:
        with silent_operation("remove_note_connections_for"):
            for ln in list(self._note_connections):
                if ln.a is note or ln.b is note:
                    with silent_operation("remove_note_connection_item"):
                        self.removeItem(ln)
                    with silent_operation("unregister_note_connection"):
                        if hasattr(ln.a, '_unregister_note_connection'):
                            ln.a._unregister_note_connection(ln)
                        if hasattr(ln.b, '_unregister_note_connection'):
                            ln.b._unregister_note_connection(ln)
                    with silent_operation("remove_from_note_connections"):
                        self._note_connections.remove(ln)

    # Note > Focus link management
    def add_note_focus_link(self, note: 'NoteNode', focus_node: 'FocusNode') -> Optional['NoteFocusConnector']:
        try:
            if note is None or focus_node is None:
                return None
            # avoid duplicates
            for nf in list(getattr(self, '_note_focus_links', [])):
                if getattr(nf, 'note', None) is note and getattr(nf, 'focus_node', None) is focus_node:
                    return nf
            d = getattr(self, 'note_defaults', {})
            col = QColor(d.get('connection_color')) if d.get('connection_color') else QColor(60, 140, 200)
            width = int(d.get('connection_width', 2))
            nf = NoteFocusConnector(note, focus_node, col, width)
            nf.setVisible(self.notes_enabled)
            self.addItem(nf)
            with silent_operation("set_note_focus_zvalue"):
                nf.setZValue(self.z_connections)
            self._note_focus_links.append(nf)
            with silent_operation("register_note_focus_connector"):
                if hasattr(note, '_note_focus_connectors') and nf not in note._note_focus_connectors:
                    note._note_focus_connectors.append(nf)
            with silent_operation("register_focus_note_connector"):
                if hasattr(focus_node, 'note_focus_connectors') and nf not in focus_node.note_focus_connectors:
                    focus_node.note_focus_connectors.append(nf)
            return nf
        except Exception:
            return None

    def remove_note_focus_links_for(self, obj) -> None:
        try:
            for nf in list(getattr(self, '_note_focus_links', [])):
                if getattr(nf, 'note', None) is obj or getattr(nf, 'focus_node', None) is obj:
                    try:
                        self.removeItem(nf)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    with silent_operation("remove_note_focus_link"):
                        self._note_focus_links.remove(nf)
                    with silent_operation("unregister_note_focus_from_note"):
                        if hasattr(nf.note, '_note_focus_connectors') and nf in nf.note._note_focus_connectors:
                            nf.note._note_focus_connectors.remove(nf)
                    with silent_operation("unregister_note_focus_from_focus"):
                        if hasattr(nf.focus_node, 'note_focus_connectors') and nf in nf.focus_node.note_focus_connectors:
                            nf.focus_node.note_focus_connectors.remove(nf)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # Note > Event link management
    def add_note_event_link(self, note: 'NoteNode', event_node: 'EventNode') -> Optional['NoteEventConnector']:
        try:
            if note is None or event_node is None:
                return None
            for ne in list(getattr(self, '_note_event_links', [])):
                if getattr(ne, 'note', None) is note and getattr(ne, 'event_node', None) is event_node:
                    return ne
            d = getattr(self, 'note_defaults', {})
            col = QColor(d.get('connection_color')) if d.get('connection_color') else QColor(140, 60, 200)
            width = int(d.get('connection_width', 2))
            ne = NoteEventConnector(note, event_node, col, width)
            # Note↔Event links are part of the notes feature; follow notes visibility
            ne.setVisible(self.notes_enabled)
            self.addItem(ne)
            with silent_operation("set_note_event_zvalue"):
                ne.setZValue(self.z_connections)
            self._note_event_links.append(ne)
            with silent_operation("register_note_event_on_note"):
                if hasattr(note, '_note_event_connectors') and ne not in note._note_event_connectors:
                    note._note_event_connectors.append(ne)
            with silent_operation("register_note_event_on_event"):
                if hasattr(event_node, 'note_event_connectors') and ne not in event_node.note_event_connectors:
                    event_node.note_event_connectors.append(ne)
            return ne
        except Exception:
            return None

    def remove_note_event_links_for(self, obj) -> None:
        with silent_operation("remove_note_event_links_for"):
            for ne in list(getattr(self, '_note_event_links', [])):
                if getattr(ne, 'note', None) is obj or getattr(ne, 'event_node', None) is obj:
                    with silent_operation("remove_note_event_item"):
                        self.removeItem(ne)
                    with silent_operation("remove_from_note_event_links"):
                        self._note_event_links.remove(ne)
                    with silent_operation("unregister_note_event_from_note"):
                        if hasattr(ne.note, '_note_event_connectors') and ne in ne.note._note_event_connectors:
                            ne.note._note_event_connectors.remove(ne)
                    with silent_operation("unregister_note_event_from_event"):
                        if hasattr(ne.event_node, 'note_event_connectors') and ne in ne.event_node.note_event_connectors:
                            ne.event_node.note_event_connectors.remove(ne)

    # Event <> Focus link management
    def add_event_focus_link(self, event_node: 'EventNode', focus_node: 'FocusNode') -> Optional['EventFocusConnector']:
        try:
            if event_node is None or focus_node is None:
                return None
            for ef in list(getattr(self, '_event_focus_links', [])):
                if getattr(ef, 'event_node', None) is event_node and getattr(ef, 'focus_node', None) is focus_node:
                    return ef
            d = getattr(self, 'note_defaults', {})
            col = QColor(d.get('connection_color')) if d.get('connection_color') else QColor(70, 160, 120)
            width = int(d.get('connection_width', 2))
            ef = EventFocusConnector(event_node, focus_node, col, width)
            # Event↔Focus links are not notes; keep them always visible (independent of notes toggle)
            ef.setVisible(True)
            self.addItem(ef)
            with silent_operation("set_event_focus_zvalue"):
                ef.setZValue(self.z_connections)
            self._event_focus_links.append(ef)
            with silent_operation("register_event_focus_on_event"):
                if hasattr(event_node, 'event_focus_connectors') and ef not in event_node.event_focus_connectors:
                    event_node.event_focus_connectors.append(ef)
            with silent_operation("register_event_focus_on_focus"):
                if hasattr(focus_node, 'event_focus_connectors') and ef not in focus_node.event_focus_connectors:
                    focus_node.event_focus_connectors.append(ef)
            # Inject a simple country_event call into the focus completion_reward so GUI shows the event
            try:
                fobj = getattr(focus_node, 'focus', None)
                evobj = getattr(event_node, 'event', None)
                if fobj is not None and evobj is not None:
                    evid = str(getattr(evobj, 'id', '') or '').strip()
                    if evid:
                        # If the focus reward doesn't already reference this event id, set a concise
                        # reference so the focus GUI shows only the event invocation. The full
                        # event script (trigger/options) is maintained on the Event object and
                        # shown/edited in the Event editor.
                        cr = str(getattr(fobj, 'completion_reward', '') or '')
                        if evid not in cr:
                            # Minimal, clear reference to the event ID only.
                            snippet = f"country_event = {{ id = {evid} }}"
                            if cr.strip():
                                new_cr = cr.rstrip() + "\n" + snippet
                            else:
                                new_cr = snippet
                            try:
                                fobj.completion_reward = new_cr
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            # Refresh any open focus editor UI and the node visuals
                            try:
                                # update focus node visuals if present in canvas
                                if hasattr(self, 'nodes') and getattr(self, 'nodes', None) and fobj.id in getattr(self, 'nodes', {}):
                                    node = self.nodes.get(fobj.id)
                                    try:
                                        node.update()
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            try:
                                # If there's an editor dialog showing this focus, update its reward_edit if present
                                editor = getattr(self, 'editor', None) if hasattr(self, 'editor') else None
                                if editor and hasattr(editor, 'active_focus_dialog') and getattr(editor, 'active_focus_dialog', None):
                                    dlg = editor.active_focus_dialog
                                    try:
                                        if getattr(dlg, 'focus', None) is fobj and hasattr(dlg, 'reward_edit'):
                                            dlg.reward_edit.setPlainText(fobj.completion_reward)
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            # Intentionally do NOT auto-populate event triggers/options/title/description.
                            # Keep events minimal; the end user will implement them in the Event editor.
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return ef
        except Exception:
            return None

    def remove_event_focus_links_for(self, obj) -> None:
        with silent_operation("remove_event_focus_links_for"):
            for ef in list(getattr(self, '_event_focus_links', [])):
                if getattr(ef, 'event_node', None) is obj or getattr(ef, 'focus_node', None) is obj:
                    with silent_operation("remove_event_focus_item"):
                        self.removeItem(ef)
                    with silent_operation("remove_from_event_focus_links"):
                        self._event_focus_links.remove(ef)
                    with silent_operation("unregister_event_focus_from_event"):
                        if hasattr(ef.event_node, 'event_focus_connectors') and ef in ef.event_node.event_focus_connectors:
                            ef.event_node.event_focus_connectors.remove(ef)
                    with silent_operation("unregister_event_focus_from_focus"):
                        if hasattr(ef.focus_node, 'event_focus_connectors') and ef in ef.focus_node.event_focus_connectors:
                            ef.focus_node.event_focus_connectors.remove(ef)

    # Event <> Event link management
    def add_event_event_link(self, a: 'EventNode', b: 'EventNode') -> Optional['EventEventConnector']:
        try:
            if a is None or b is None or a is b:
                return None
            # avoid duplicates by object pair
            pair = tuple(sorted([id(a), id(b)]))
            for ee in list(getattr(self, '_event_event_links', [])):
                if tuple(sorted([id(getattr(ee, 'a', None)), id(getattr(ee, 'b', None))])) == pair:
                    return ee
            d = getattr(self, 'note_defaults', {})
            col = QColor(d.get('connection_color')) if d.get('connection_color') else QColor(160, 120, 70)
            width = int(d.get('connection_width', 2))
            ee = EventEventConnector(a, b, col, width)
            ee.setVisible(self.notes_enabled)
            self.addItem(ee)
            with silent_operation("set_event_event_zvalue"):
                ee.setZValue(self.z_connections)
            self._event_event_links.append(ee)
            with silent_operation("register_event_event_on_a"):
                if hasattr(a, 'event_event_connectors') and ee not in a.event_event_connectors:
                    a.event_event_connectors.append(ee)
            with silent_operation("register_event_event_on_b"):
                if hasattr(b, 'event_event_connectors') and ee not in b.event_event_connectors:
                    b.event_event_connectors.append(ee)
            return ee
        except Exception:
            return None

    def remove_event_event_links_for(self, obj) -> None:
        try:
            for ee in list(getattr(self, '_event_event_links', [])):
                if getattr(ee, 'a', None) is obj or getattr(ee, 'b', None) is obj:
                    try:
                        self.removeItem(ee)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self._event_event_links.remove(ee)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        if hasattr(ee.a, 'event_event_connectors') and ee in ee.a.event_event_connectors:
                            ee.a.event_event_connectors.remove(ee)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        if hasattr(ee.b, 'event_event_connectors') and ee in ee.b.event_event_connectors:
                            ee.b.event_event_connectors.remove(ee)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # Unified add_link/remove_links_for for arbitrary chains
    def add_link(self, a: QGraphicsItem, b: QGraphicsItem):
        """Create a visual non-prereq link between any two supported node types.

        Supported pairs:
        - NoteNode ↔ NoteNode
        - NoteNode ↔ FocusNode
        - NoteNode ↔ EventNode
        - EventNode ↔ FocusNode
        - EventNode ↔ EventNode
        """
        try:
            from_a, from_b = a, b
            # funnel to specific helpers
            if isinstance(from_a, NoteNode) and isinstance(from_b, NoteNode):
                return self.add_note_connection(from_a, from_b)
            if isinstance(from_a, NoteNode) and isinstance(from_b, FocusNode):
                return self.add_note_focus_link(from_a, from_b)
            if isinstance(from_a, FocusNode) and isinstance(from_b, NoteNode):
                return self.add_note_focus_link(from_b, from_a)
            if isinstance(from_a, NoteNode) and isinstance(from_b, EventNode):
                return self.add_note_event_link(from_a, from_b)
            if isinstance(from_a, EventNode) and isinstance(from_b, NoteNode):
                return self.add_note_event_link(from_b, from_a)
            if isinstance(from_a, EventNode) and isinstance(from_b, FocusNode):
                return self.add_event_focus_link(from_a, from_b)
            if isinstance(from_a, FocusNode) and isinstance(from_b, EventNode):
                return self.add_event_focus_link(from_b, from_a)
            if isinstance(from_a, EventNode) and isinstance(from_b, EventNode):
                return self.add_event_event_link(from_a, from_b)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return None

    def remove_links_for(self, obj: QGraphicsItem) -> None:
        try:
            # existing removals
            if hasattr(self, 'remove_note_connections_for') and isinstance(obj, NoteNode):
                self.remove_note_connections_for(obj)
            if hasattr(self, 'remove_note_focus_links_for') and (isinstance(obj, NoteNode) or isinstance(obj, FocusNode)):
                self.remove_note_focus_links_for(obj)
            if hasattr(self, 'remove_note_event_links_for') and (isinstance(obj, NoteNode) or isinstance(obj, EventNode)):
                self.remove_note_event_links_for(obj)
            # new removals
            if hasattr(self, 'remove_event_focus_links_for') and (isinstance(obj, EventNode) or isinstance(obj, FocusNode)):
                self.remove_event_focus_links_for(obj)
            if hasattr(self, 'remove_event_event_links_for') and isinstance(obj, EventNode):
                self.remove_event_event_links_for(obj)
            # FocusNode prerequisite links (parents/children) are handled
            # by explicit menu actions so they don't get removed by the
            # generic remove_links_for call. Provide helpers below to
            # remove incoming (parents) or outgoing (children) prereq links.
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def remove_focus_parent_links_for(self, node: 'FocusNode') -> None:
        """Remove incoming prerequisite links (parents) for the given FocusNode.

        This removes visual connections where other nodes point to `node` and
        also removes the corresponding entries from the focus.prerequisites list.
        """
        try:
            # iterate a snapshot because remove_connection mutates the lists
            for conn in list(getattr(node, 'connections_in', []) or []):
                try:
                    # remove prereq entry on this node (end node)
                    start = getattr(getattr(conn, 'start_node', None), 'focus', None)
                    end = getattr(getattr(conn, 'end_node', None), 'focus', None)
                    if end is not None and start is not None:
                        sid = getattr(start, 'id', None)
                        try:
                            if sid in end.prerequisites:
                                end.prerequisites = [p for p in end.prerequisites if p != sid]
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.remove_connection(conn)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def remove_focus_child_links_for(self, node: 'FocusNode') -> None:
        """Remove outgoing prerequisite links (children) for the given FocusNode.

        This removes visual connections from `node` to its children and also
        removes the corresponding prerequisite entries from each child focus.
        """
        try:
            for conn in list(getattr(node, 'connections_out', []) or []):
                try:
                    start = getattr(getattr(conn, 'start_node', None), 'focus', None)
                    end = getattr(getattr(conn, 'end_node', None), 'focus', None)
                    if start is not None and end is not None:
                        sid = getattr(start, 'id', None)
                        try:
                            if sid in end.prerequisites:
                                end.prerequisites = [p for p in end.prerequisites if p != sid]
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.remove_connection(conn)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def add_event_node(self, event: Event):
        """Add an EventNode to the canvas at either grid or free coordinates."""
        try:
            node = EventNode(event, self.parent)
            # prefer free placement when present
            if event.free_x is not None and event.free_y is not None:
                node.setPos(float(event.free_x), float(event.free_y))
            else:
                node.setPos(event.x * GRID_UNIT, event.y * GRID_UNIT)
            try:
                node.setZValue(self.z_nodes)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.addItem(node)
            # store reference for linking and lookups
            try:
                if getattr(event, 'id', None):
                    self.event_nodes[event.id] = node
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return node
        except Exception as e:
            logger.error("[canvas] Error in add_event_node for %s: %s", getattr(event, 'id', None), e)
            return None

    def set_grid_visible(self, visible: bool) -> None:
        """Toggle grid visibility and redraw if needed."""
        with silent_operation("set_grid_visible"):
            vis = bool(visible)
            if getattr(self, '_grid_visible', True) == vis:
                return
            self._grid_visible = vis
            self.draw_grid()

    def compute_palette_for_networks(self, network_ids: List[int], seed: Optional[int] = None) -> None:
        """Compute and set `network_colors` for the given network ids.

        Colors are chosen from HSL evenly around the hue wheel using a deterministic
        mapping from network id and optional seed so results are repeatable.
        """
        self._palette_seed = seed
        ids = sorted(set(network_ids))
        n = max(1, len(ids))
        for idx, net in enumerate(ids):
            # compute hue offset deterministically
            base = (hash((net, seed)) & 0xffffffff) / 0xffffffff
            hue = (base * 360.0 + idx * (360.0 / n)) % 360.0
            sat = 0.55 + ((hash((net, 's')) & 0xff) / 0xff) * 0.35
            light = 0.45 + ((hash((net, 'l')) & 0xff) / 0xff) * 0.15
            color = QColor()
            color.setHslF(hue / 360.0, max(0.2, min(1.0, sat)), max(0.15, min(0.85, light)), 1.0)
            self.network_colors[net] = color
        # SuperNet color distinct (dark/neutral)
        self.network_colors[-1] = QColor(30, 30, 30)
        # ensure layer colors exist for at least layer 0
        if 0 not in self.layer_colors:
            self.layer_colors[0] = QColor(200, 200, 200)

    def add_focus_node(self, focus: Focus):
        """Add a new focus node to the canvas"""
        try:
            logger.debug("[canvas] Starting add_focus_node for: %s", focus.id)
            node = FocusNode(focus, self.parent)
            logger.debug("[canvas] Created FocusNode successfully")

            node.setPos(focus.x * GRID_UNIT, focus.y * GRID_UNIT)
            logger.debug("[canvas] Set position to: (%s, %s)", focus.x * GRID_UNIT, focus.y * GRID_UNIT)

            # Ensure nodes render above connections and grid
            try:
                node.setZValue(self.z_nodes)
                logger.debug("[canvas] Set Z-value to %s", self.z_nodes)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            self.addItem(node)
            logger.debug("[canvas] Added node to scene")
            try:
                if hasattr(self, '_spatial_index'):
                    self._spatial_index.insert(node, node.sceneBoundingRect())
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if hasattr(self, '_visible_nodes_cache'):
                    self._visible_nodes_cache.add(node)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                sp = node.scenePos()
                logger.debug("[canvas] added node id=%s grid=(%s,%s) pos=(%.1f,%.1f)", focus.id, focus.x, focus.y, sp.x(), sp.y())
            except Exception:
                logger.debug("[canvas] added node id=%s grid=(%s,%s)", focus.id, focus.x, focus.y)
            # Register node in the canvas node lookup so connection creation can find it
            try:
                key = str(getattr(focus, 'id', '') or '').strip()
                if key:
                    # ensure nodes mapping exists
                    if not isinstance(getattr(self, 'nodes', None), dict):
                        try:
                            self.nodes = {}
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.nodes[key] = node
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Hidden-branch handling: register node under hidden tags and set initial visibility
            try:
                tags = list(getattr(focus, 'hidden_tags', []) or [])
                is_hidden_flag = bool(getattr(focus, 'hidden', False))
                # Register node under each tag for later toggles
                for t in tags:
                    try:
                        lst = self._hidden_tag_index.get(t)
                        if lst is None:
                            lst = []
                            self._hidden_tag_index[t] = lst
                        if node not in lst:
                            lst.append(node)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Determine initial visibility: if focus marked hidden, hide unless a tag is explicitly enabled
                try:
                    visible = True
                    if is_hidden_flag:
                        visible = False
                        for t in tags:
                            try:
                                if self._show_hidden_branches_by_tag.get(t, False):
                                    visible = True
                                    break
                            except Exception:
                                continue
                    node.setVisible(bool(visible))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                # mark frames dirty so layer frames will be recomputed
                self._frames_dirty = True
                self.schedule_frame_update()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.refresh_mutex_connectors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            logger.debug("[canvas] Completed add_focus_node for: %s", focus.id)
            return node
        except Exception as e:
            logger.error("[canvas] Error in add_focus_node for %s: %s", focus.id, e)
            import traceback
            logger.error("[canvas] Traceback: %s", traceback.format_exc())
            raise

    def sync_focus_positions(self) -> None:
        """Ensure each Focus object's x/y reflect its node's actual scene position.

        This rounds scene coordinates to the nearest GRID_UNIT grid cell and writes
        the values back into the Focus dataclass so save/export routines read the
        latest positions from the model.
        """
        with silent_operation("sync_focus_positions"):
            for fid, node in list(self.nodes.items()):
                with silent_operation("sync_single_focus_position"):
                    pos = node.scenePos()
                    gx = round(pos.x() / GRID_UNIT)
                    gy = round(pos.y() / GRID_UNIT)
                    if hasattr(node, 'focus') and node.focus is not None:
                        node.focus.x = int(gx)
                        node.focus.y = int(gy)

    # -------------------------
    # Frame management
    # -------------------------
    def clear_frames(self):
        """Remove all frame items from the scene."""
        for f in list(self.frames):
            try:
                self.removeItem(f)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.frames.clear()
        # reset dirty flag when clearing
        self._frames_dirty = True

    def add_frame(self, rect: QRectF, color: QColor, margin: float = 20.0, z: int = -5, label: str = None, tooltip: str = None):
        """Add a semi-transparent frame rectangle behind nodes/connections.

        rect: QRectF in scene coordinates
        color: QColor for the fill/outline
        margin: extra padding (pixels)
        z: z-value for layering (should be below nodes but above grid)
        label: optional text label to display in corner
        tooltip: optional tooltip text
        """
        r = QRectF(rect)
        r.adjust(-margin, -margin, margin, margin)
        item = QGraphicsRectItem(r)

        # Determine variant index for the same network color if possible
        try:
            net_key = None
            for k, v in self.network_colors.items():
                if isinstance(v, QColor) and v == color:
                    net_key = k
                    break
        except Exception:
            net_key = None

        variant_index = 0
        if net_key is not None:
            cnt = self._frame_variant_counters.get(net_key, 0)
            variant_index = cnt
            self._frame_variant_counters[net_key] = cnt + 1

        def _shade_variant(base: QColor, idx: int, total: int = 6) -> QColor:
            try:
                h, s, l, a = base.getHslF()
            except Exception:
                try:
                    h = base.hslHueF() or 0.0
                except Exception:
                    h = 0.0
                try:
                    s = base.hslSaturationF() or 0.6
                except Exception:
                    s = 0.6
                try:
                    l = base.lightnessF() or 0.5
                except Exception:
                    l = 0.5
                a = base.alphaF() if hasattr(base, 'alphaF') else 1.0
            spread = 0.12
            if total <= 1:
                offset = 0.0
            else:
                offset = ((idx % total) / (total - 1)) * spread - (spread / 2.0)
            new_l = max(0.05, min(0.95, l + offset))
            c = QColor()
            c.setHslF(h, max(0.0, min(1.0, s)), new_l, a)
            return c

        var_color = _shade_variant(color, variant_index, total=6)
        brush = QBrush(var_color)
        brush.setStyle(Qt.BrushStyle.SolidPattern)
        fill_color = QColor(var_color)
        fill_color.setAlpha(40 + min(80, variant_index * 6))
        brush.setColor(fill_color)
        item.setBrush(brush)
        pen = QPen(var_color)
        pen.setWidth(2)
        item.setPen(pen)
        try:
            item.setZValue(z if z is not None else self.z_background)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.addItem(item)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.frames.append(item)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        
        # Add text label if requested and labels are enabled
        if label and self.show_frame_labels:
            try:
                from PyQt6.QtWidgets import QGraphicsTextItem
                text_item = QGraphicsTextItem(label)
                text_item.setDefaultTextColor(QColor(255, 255, 255, 200))
                text_item.setZValue(z + 0.5 if z is not None else self.z_background + 0.5)
                # Position in top-left corner with small padding
                text_item.setPos(r.left() + 8, r.top() + 4)
                # Add semi-transparent background for readability
                font = text_item.font()
                font.setPointSize(9)
                font.setBold(True)
                text_item.setFont(font)
                self.addItem(text_item)
                self.frames.append(text_item)
                # Store reference on the rectangle item for cleanup
                item.setData(0, text_item)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        
        # Add tooltip if provided
        if tooltip:
            try:
                item.setToolTip(tooltip)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        
        return item

    def update_frames(self):
        """Compute frames per network and add them to the scene.

        Current policy:
         - one frame per network that groups all nodes with same network_id
         - fallback network_id None nodes are ignored
        """
        if not self.frames_enabled:
            self.clear_frames()
            return

        # If frames are not dirty, still refresh visibility and return early
        if not getattr(self, '_frames_dirty', True):
            # ensure cached layer items visibility matches settings
            for lid, item in list(self._layer_frame_items.items()):
                vis = self.layer_visibility.get(lid, True)
                try:
                    item.setVisible(vis)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return
        # remove old frames (we'll reuse cached layer items separately)
        self.clear_frames()
        if not self.nodes:
            return

        # ensure lineage groups are current
        try:
            self.recompute_lineages()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # group node bounding rects by network_id, primary parent subtree, and subtree_layer
        groups: Dict[int, QRectF] = {}
        subtree_groups: Dict[str, QRectF] = {}
        layer_groups: Dict[int, QRectF] = {}
        lineage_groups: Dict[str, QRectF] = {}

        # compute primary parent mapping similar to generator policy: parent at y-1
        id_to_node = {n.focus.id: n for n in self.nodes.values()}
        for node in self.nodes.values():
            net = getattr(node.focus, 'network_id', None)
            if net is not None:
                nb = node.sceneBoundingRect()
                if net not in groups:
                    groups[net] = QRectF(nb)
                else:
                    groups[net] = groups[net].united(nb)

            # group by subtree_layer if present
            try:
                layer = int(getattr(node.focus, 'subtree_layer', 0))
            except Exception:
                layer = 0
            nb = node.sceneBoundingRect()
            if layer not in layer_groups:
                layer_groups[layer] = QRectF(nb)
            else:
                layer_groups[layer] = layer_groups[layer].united(nb)

            # primary parent heuristic
            for p in node.focus.prerequisites:
                parent_node = id_to_node.get(p)
                if parent_node and parent_node.scenePos().y() == node.scenePos().y() - GRID_UNIT:
                    # group under parent id
                    if p not in subtree_groups:
                        subtree_groups[p] = QRectF(node.sceneBoundingRect())
                    else:
                        subtree_groups[p] = subtree_groups[p].united(node.sceneBoundingRect())

        # Draw lineage frames (deepest, behind everything)
        # prefer precomputed lineage rects if available
        if self.show_lineage_frames:
            try:
                lineage_groups = dict(self._precomputed_lineage_rects)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for lid, rect in lineage_groups.items():
                base = self._lineage_colors.get(lid, QColor(80, 80, 80))
                label = f"Lineage {lid}" if lid else "Lineage"
                tooltip = f"Lineage group {lid}\nContains all focuses in this lineage branch"
                self.add_frame(rect, base, margin=36.0, z=-7, label=label, tooltip=tooltip)

        # Draw layer frames (very deep, one per subtree_layer). Assign z-values so deeper layers are further back
        if self.show_layer_frames:
            try:
                # sort layers ascending so layer 0 is deepest
                for li, layer in enumerate(sorted(layer_groups.keys())):
                    rect = layer_groups[layer]
                    # derive or reuse color
                    if layer not in self.layer_colors:
                        hue = (hash(layer) & 0xffff) / 0xffff
                        col = QColor()
                        col.setHslF(float(hue), 0.6, 0.5, 1.0)
                        self.layer_colors[layer] = col
                    else:
                        col = self.layer_colors[layer]
                    # check visibility setting
                    vis = self.layer_visibility.get(layer, True)
                    # Reuse existing frame item if present
                    if layer in self._layer_frame_items:
                        item = self._layer_frame_items[layer]
                        try:
                            item.setRect(QRectF(rect).adjusted(-40.0 - (li * 4.0), -40.0 - (li * 4.0), 40.0 + (li * 4.0), 40.0 + (li * 4.0)))
                            item.setBrush(QBrush(QColor(col.red(), col.green(), col.blue(), max(16, 40))))
                            item.setPen(QPen(col, 2))
                            item.setZValue(-9 - li)
                            item.setVisible(vis)
                            # Update tooltip
                            tooltip = f"Layer {layer}\nSubtree depth level\nContains {len([n for n in self.nodes.values() if getattr(n.focus, 'subtree_layer', 0) == layer])} focuses"
                            item.setToolTip(tooltip)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        self.addItem(item)
                        self.frames.append(item)
                    else:
                        label = f"Layer {layer}"
                        tooltip = f"Layer {layer}\nSubtree depth level\nContains {len([n for n in self.nodes.values() if getattr(n.focus, 'subtree_layer', 0) == layer])} focuses"
                        item = self.add_frame(rect, col, margin=40.0 + (li * 4.0), z=-9 - li, label=label, tooltip=tooltip)
                        try:
                            item.setVisible(vis)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # cache for reuse
                        self._layer_frame_items[layer] = item
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Draw network frames (behind)
        if self.show_network_frames:
            for net, rect in groups.items():
                color = self.network_colors.get(net, QColor(Qt.GlobalColor.lightGray))
                # Count focuses in this network
                focus_count = len([n for n in self.nodes.values() if getattr(n.focus, 'network_id', None) == net])
                label = f"Network {net}"
                tooltip = f"Network {net}\nContains {focus_count} focus{'es' if focus_count != 1 else ''}"
                self.add_frame(rect, color, margin=30.0, z=-6, label=label, tooltip=tooltip)

        # Draw subtree frames (nested, above network frames but below nodes)
        if self.show_subtree_frames:
            for parent_id, rect in subtree_groups.items():
                # choose a distinct color (use network color of parent if available, else red)
                parent_node = id_to_node.get(parent_id)
                if parent_node:
                    net = getattr(parent_node.focus, 'network_id', None)
                    base_color = self.network_colors.get(net, QColor(Qt.GlobalColor.red))
                    parent_name = getattr(parent_node.focus, 'id', parent_id)
                else:
                    base_color = QColor(Qt.GlobalColor.red)
                    parent_name = parent_id
                # Count children in this subtree
                child_count = len([p for p, r in subtree_groups.items() if p == parent_id])
                label = f"Subtree: {parent_name}"
                tooltip = f"Subtree under focus {parent_name}\nGroups direct children at y+1"
                # slightly darker, smaller margin
                self.add_frame(rect, base_color, margin=12.0, z=-5, label=label, tooltip=tooltip)

        # refresh connection colors in case network->color mapping changed
        try:
            self.refresh_connection_colors()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # frames are now clean
        self._frames_dirty = False

    def schedule_frame_update(self):
        """Start or restart the frame update timer to throttle updates while dragging."""
        with silent_operation("schedule_frame_update"):
            if not self.frames_enabled:
                return
            if getattr(self, '_frames_transition_in_progress', False):
                # suppress scheduling while a transition (enable/disable frames) is in progress
                return
            # mark frames dirty so the timer callback will rebuild them
            self._frames_dirty = True
            if self._frame_timer.isActive():
                self._frame_timer.stop()
            self._frame_timer.start()

    def refresh_connection_colors(self):
        """Update colors for all existing connections based on current node network_id and palette."""
        for conn in list(self.connections):
            try:
                if hasattr(conn, 'start_node') and hasattr(conn, 'end_node'):
                    if getattr(self, 'visualizer_lineage_mode', False):
                        # per-connection lineage derived from deepest leaf below the child
                        try:
                            if getattr(self, '_leaf_dirty', True):
                                self.recompute_leaf_lineages()
                            child_id = conn.end_node.focus.id
                            lid = self._leaf_of_node.get(child_id)
                            base = self._leaf_colors.get(lid)
                            if base is None:
                                base = QColor(Qt.GlobalColor.darkCyan)
                        except Exception:
                            base = QColor(Qt.GlobalColor.darkCyan)
                    elif self.color_lines_by_lineage and self._lineage_of_node:
                        # color by lineage of end node primarily
                        lid = self._lineage_of_node.get(conn.end_node.focus.id) or self._lineage_of_node.get(conn.start_node.focus.id)
                        base = self._lineage_colors.get(lid) if lid else None
                        if base is None:
                            base = QColor(Qt.GlobalColor.blue)
                    else:
                        net_id = getattr(conn.end_node.focus, 'network_id', None)
                        if net_id is None:
                            net_id = getattr(conn.start_node.focus, 'network_id', None)
                        if net_id is None:
                            net_id = 0
                        base = self.network_colors.get(net_id, None)
                        if base is None:
                            base = QColor(Qt.GlobalColor.blue)
                    # darker stroke for visibility
                    # If this connection represents a prerequisite group with explicit style, prefer that
                    try:
                        kind = getattr(conn, 'prereq_kind', None)
                    except Exception:
                        kind = None
                    if kind:
                        try:
                            # Let the connection set its prereq visual style (dash + color)
                            if hasattr(conn, 'set_prereq_style'):
                                conn.set_prereq_style(kind)
                            else:
                                conn.set_color(QColor(max(0, int(base.red() * 0.8)), max(0, int(base.green() * 0.8)), max(0, int(base.blue() * 0.8))))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    else:
                        stroke = QColor(max(0, int(base.red() * 0.8)), max(0, int(base.green() * 0.8)), max(0, int(base.blue() * 0.8)))
                        conn.set_color(stroke)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # end refresh_connection_colors

        # Also refresh colors for cross-type connectors (event/focus, event/event, note links)
        try:
            # Event->Focus connectors
            for ef in list(getattr(self, '_event_focus_links', [])):
                try:
                    e = getattr(getattr(ef, 'event_node', None), 'event', None)
                    f = getattr(getattr(ef, 'focus_node', None), 'focus', None)
                    base = None
                    if getattr(self, 'visualizer_lineage_mode', False):
                        try:
                            if getattr(self, '_leaf_dirty', True):
                                self.recompute_leaf_lineages()
                            lid = self._leaf_of_node.get(getattr(f, 'id', None)) if f is not None else None
                            base = self._leaf_colors.get(lid)
                        except Exception:
                            base = QColor(Qt.GlobalColor.darkCyan)
                    elif self.color_lines_by_lineage and self._lineage_of_node:
                        lid = None
                        if f is not None:
                            lid = self._lineage_of_node.get(getattr(f, 'id', None))
                        if lid is None and e is not None:
                            lid = self._lineage_of_node.get(getattr(e, 'id', None))
                        base = self._lineage_colors.get(lid) if lid else None
                        if base is None:
                            base = QColor(Qt.GlobalColor.green)
                    else:
                        # fallback to network coloring (prefer focus then event)
                        net_id = getattr(f, 'network_id', None) if f is not None else None
                        if net_id is None and e is not None:
                            net_id = getattr(e, 'network_id', None)
                        base = self.network_colors.get(net_id, None)
                        if base is None:
                            base = QColor(Qt.GlobalColor.green)
                    stroke = QColor(max(0, int(base.red() * 0.8)), max(0, int(base.green() * 0.8)), max(0, int(base.blue() * 0.8)))
                    ef.set_color(stroke)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        try:
            # Event->Event connectors
            for ee in list(getattr(self, '_event_event_links', [])):
                try:
                    a_ev = getattr(getattr(ee, 'a', None), 'event', None)
                    b_ev = getattr(getattr(ee, 'b', None), 'event', None)
                    base = None
                    if getattr(self, 'visualizer_lineage_mode', False):
                        try:
                            if getattr(self, '_leaf_dirty', True):
                                self.recompute_leaf_lineages()
                            lid = self._leaf_of_node.get(getattr(b_ev, 'id', None)) if b_ev is not None else None
                            base = self._leaf_colors.get(lid)
                        except Exception:
                            base = QColor(Qt.GlobalColor.darkCyan)
                    elif self.color_lines_by_lineage and self._lineage_of_node:
                        lid = self._lineage_of_node.get(getattr(b_ev, 'id', None)) or self._lineage_of_node.get(getattr(a_ev, 'id', None))
                        base = self._lineage_colors.get(lid) if lid else None
                        if base is None:
                            base = QColor(Qt.GlobalColor.darkYellow)
                    else:
                        net_id = getattr(b_ev, 'network_id', None) if b_ev is not None else None
                        if net_id is None and a_ev is not None:
                            net_id = getattr(a_ev, 'network_id', None)
                        base = self.network_colors.get(net_id, None)
                        if base is None:
                            base = QColor(Qt.GlobalColor.darkYellow)
                    stroke = QColor(max(0, int(base.red() * 0.8)), max(0, int(base.green() * 0.8)), max(0, int(base.blue() * 0.8)))
                    ee.set_color(stroke)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        try:
            # Note connectors: use muted colors but still respect lineage/root network if possible
            for nf in list(getattr(self, '_note_focus_links', [])):
                try:
                    f = getattr(getattr(nf, 'focus_node', None), 'focus', None)
                    base = None
                    if self.color_lines_by_lineage and self._lineage_of_node:
                        lid = self._lineage_of_node.get(getattr(f, 'id', None)) if f is not None else None
                        base = self._lineage_colors.get(lid) if lid else None
                    if base is None:
                        base = QColor(90, 90, 90)
                    nf.set_color(QColor(max(0, int(base.red() * 0.6)), max(0, int(base.green() * 0.6)), max(0, int(base.blue() * 0.6))))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for ne in list(getattr(self, '_note_event_links', [])):
                try:
                    e = getattr(getattr(ne, 'event_node', None), 'event', None)
                    base = None
                    if self.color_lines_by_lineage and self._lineage_of_node:
                        lid = self._lineage_of_node.get(getattr(e, 'id', None)) if e is not None else None
                        base = self._lineage_colors.get(lid) if lid else None
                    if base is None:
                        base = QColor(120, 80, 190)
                    ne.set_color(QColor(max(0, int(base.red() * 0.6)), max(0, int(base.green() * 0.6)), max(0, int(base.blue() * 0.6))))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def rebuild_connection_styles(self, *, recompute_groups: bool = True, recolor: bool = True, refresh_mutex: bool = False) -> None:
        """Restyle prerequisite connections to match HOI4 visuals after bulk updates."""
        if getattr(self, '_rebuilding_connection_styles', False):
            return
        self._rebuilding_connection_styles = True
        try:
            if recompute_groups:
                try:
                    self._apply_prereq_group_styles()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                for conn in list(self.connections):
                    try:
                        kind = getattr(conn, 'prereq_kind', None)
                        if kind and hasattr(conn, 'set_prereq_style'):
                            conn.set_prereq_style(kind)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            if recolor:
                try:
                    self.refresh_connection_colors()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            if refresh_mutex:
                try:
                    self.refresh_mutex_connectors()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        finally:
            try:
                self._rebuilding_connection_styles = False
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _apply_prereq_group_styles(self) -> None:
        """Second-pass: ensure existing connections reflect prerequisite group kinds from focuses.

        This is a robust recovery run used after loading the project to re-assign
        prereq_kind on connections and call set_prereq_style so orange/yellow dashed
        lines are shown for OR/AND groups respectively.
        """
        try:
            canvas = getattr(self, 'canvas', self)
            # Fallback recovery: build a lightweight (child -> {parent: kind}) mapping
            # In normal loads this mapping is constructed earlier and applied; keep this
            # function as a safe second-pass to repair any missing styling.
            mapping = {}
            for f in getattr(self, 'focuses', []) or []:
                try:
                    groups = getattr(f, 'prerequisites_groups', []) or []
                    if not groups:
                        continue
                    child = getattr(f, 'id', None)
                    if not child:
                        continue
                    child = str(child).strip()
                    parent_map = mapping.setdefault(child, {})
                    for g in groups:
                        try:
                            typ = (g.get('type') or 'AND').upper() if isinstance(g, dict) else 'AND'
                            items = list(g.get('items', []) if isinstance(g, dict) else [])
                            for pid in items:
                                if not pid:
                                    continue
                                npid = str(pid).strip()
                                if not npid:
                                    continue
                                if npid not in parent_map:
                                    parent_map[npid] = typ
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Iterate existing connections and apply styles where mapping indicates
            for conn in list(getattr(canvas, 'connections', [])):
                try:
                    if not (hasattr(conn, 'start_node') and hasattr(conn, 'end_node')):
                        continue
                    s = getattr(getattr(conn, 'start_node', None), 'focus', None)
                    e = getattr(getattr(conn, 'end_node', None), 'focus', None)
                    if s is None or e is None:
                        continue
                    child_id = getattr(e, 'id', None)
                    parent_id = getattr(s, 'id', None)
                    try:
                        child_id = str(child_id).strip()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        parent_id = str(parent_id).strip()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    if not child_id or not parent_id:
                        continue
                    kind = mapping.get(child_id, {}).get(parent_id)
                    if kind:
                        try:
                            conn.prereq_kind = kind
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        if hasattr(conn, 'set_prereq_style'):
                            try:
                                conn.set_prereq_style(kind)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Create missing connections for any declared group parents that lack a visual
            try:
                for child_id, parents in mapping.items():
                    try:
                        # ensure child node exists
                        if child_id not in getattr(canvas, 'nodes', {}):
                            continue
                        for parent_id, kind in list(parents.items()):
                            try:
                                if parent_id not in getattr(canvas, 'nodes', {}):
                                    continue
                                # check if connection exists
                                exists = None
                                for conn in list(getattr(canvas, 'connections', [])):
                                    try:
                                        s = getattr(getattr(conn, 'start_node', None), 'focus', None)
                                        e = getattr(getattr(conn, 'end_node', None), 'focus', None)
                                        if s is None or e is None:
                                            continue
                                        if str(getattr(s, 'id', '')).strip() == parent_id and str(getattr(e, 'id', '')).strip() == child_id:
                                            exists = conn
                                            break
                                    except Exception:
                                        continue
                                if exists is None:
                                    try:
                                        line = canvas.create_connection(parent_id, child_id)
                                        if line is not None:
                                            try:
                                                line.prereq_kind = kind
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                            if hasattr(line, 'set_prereq_style'):
                                                try:
                                                    line.set_prereq_style(kind)
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                else:
                                    # ensure style on existing
                                    try:
                                        exists.prereq_kind = kind
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    if hasattr(exists, 'set_prereq_style'):
                                        try:
                                            exists.set_prereq_style(kind)
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def create_connection(self, from_id, to_id, prereq_kind: Optional[str] = None):
        """Create an L-shaped connection between two focuses

        New optional signature supports `prereq_kind` to style prerequisite connections:
            create_connection(from_id, to_id, prereq_kind='AND'|'OR'|None)
        """
        # Normalize ids to strings without surrounding whitespace for robust lookup
        try:
            fid = None if from_id is None else str(from_id)
        except Exception:
            fid = from_id
        try:
            tid = None if to_id is None else str(to_id)
        except Exception:
            tid = to_id
        try:
            fid = fid.strip() if isinstance(fid, str) else fid
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            tid = tid.strip() if isinstance(tid, str) else tid
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        if fid in self.nodes and tid in self.nodes:
            from_node = self.nodes[fid]
            to_node = self.nodes[tid]
            # Optional debug: record positions & IDs to help diagnose creation-order issues
            try:
                if getattr(self, 'debug_connection_creation', False):
                    try:
                        sp = from_node.scenePos()
                        ep = to_node.scenePos()
                        logger.debug("[conn_debug] create_connection from=%r(%s) to=%r(%s) start_pos=(%.1f,%.1f) end_pos=(%.1f,%.1f)", fid, getattr(from_node, 'focus', None) and getattr(from_node.focus, 'name', None), tid, getattr(to_node, 'focus', None) and getattr(to_node.focus, 'name', None), sp.x(), sp.y(), ep.x(), ep.y())
                    except Exception:
                        logger.debug("[conn_debug] create_connection from=%r to=%r (couldn't read positions)", fid, tid)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # prereq_kind is accepted directly; callers should pass it when known
            line = LShapedConnectionLine(from_node, to_node)
            # Determine color per current mode
            if getattr(self, 'visualizer_lineage_mode', False):
                try:
                    if getattr(self, '_leaf_dirty', True):
                        self.recompute_leaf_lineages()
                    lid = self._leaf_of_node.get(to_node.focus.id)
                    col = self._leaf_colors.get(lid, QColor(Qt.GlobalColor.darkCyan))
                except Exception:
                    col = QColor(Qt.GlobalColor.darkCyan)
                line.set_color(col)
            elif self.color_lines_by_lineage and self._lineage_of_node:
                lid = self._lineage_of_node.get(to_node.focus.id) or self._lineage_of_node.get(from_node.focus.id)
                col = self._lineage_colors.get(lid, QColor(Qt.GlobalColor.blue))
                line.set_color(col)
            else:
                net_id = getattr(to_node.focus, 'network_id', None)
                if net_id is None:
                    net_id = getattr(from_node.focus, 'network_id', None)
                if net_id is None:
                    # default to 0
                    net_id = 0
                if isinstance(net_id, int) and net_id in self.network_colors:
                    line.set_color(self.network_colors[net_id])
            self.addItem(line)
            try:
                line.setZValue(self.z_connections)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # attach prereq_kind on the line for downstream refresh logic and set style
            try:
                line.prereq_kind = prereq_kind
                if prereq_kind and hasattr(line, 'set_prereq_style'):
                    line.set_prereq_style(prereq_kind)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Track connection globally and in nodes
            self.connections.append(line)
            try:
                from_node.connections_out.append(line)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                to_node.connections_in.append(line)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                logger.debug("[canvas] created connection %s -> %s", from_id, to_id)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # lineage might have changed, refresh derived data
            try:
                self.recompute_lineages()
                self._leaf_dirty = True
                self.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.refresh_mutex_connectors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # schedule reflow: nodes may have transitioned between isolated/connected
            try:
                try:
                    if getattr(self, 'auto_layout_enabled', False):
                        if getattr(self, '_layout_in_progress', False):
                            # don't schedule while a layout is running
                            pass
                        else:
                            if getattr(self, 'auto_layout_enabled', False):
                                if getattr(self, 'auto_layout_enabled', False):
                                    if not self._reflow_timer.isActive():
                                        self._reflow_timer.start()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception:
                try:
                    if getattr(self, 'auto_layout_enabled', False):
                        if getattr(self, 'auto_layout_enabled', False):
                            if getattr(self, 'auto_layout_enabled', False):
                                self.reflow_unconnected_nodes()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return line
    def set_visualizer_lineage_mode(self, enabled: bool):
        """Atomically toggle visualizer lineage mode.

        While a transition is in progress, intermediate frame update
        scheduling is suppressed to avoid remove/add churn that causes
        visible flashing. A single consolidated refresh is performed
        and then normal scheduling is re-enabled.
        """
        try:
            # suppress intermediate scheduling
            self._frames_transition_in_progress = True
            # store the new mode
            self.visualizer_lineage_mode = bool(enabled)
            if enabled:
                # remember current frames_enabled state and hide frames
                self._prev_frames_enabled = getattr(self, '_prev_frames_enabled', self.frames_enabled)
                self.frames_enabled = False
                # remove existing frames immediately
                try:
                    self.clear_frames()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # mark leaf lineage dirty and optionally compact layout for clearer view
                self._leaf_dirty = True
                try:
                    if getattr(self, 'auto_layout_enabled', False):
                        self.compact_connected_layout()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                # restore previous frames_enabled and mark frames dirty
                try:
                    self.frames_enabled = getattr(self, '_prev_frames_enabled', True)
                except Exception:
                    self.frames_enabled = True
                self._frames_dirty = True
                # schedule a single frames update (will be suppressed until we clear the transition flag)
                try:
                    self.schedule_frame_update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # refresh connection colors now that mode changed
            try:
                self.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        finally:
            # Ensure we clear the transition flag shortly after so normal scheduling resumes
            try:
                QTimer.singleShot(80, lambda: setattr(self, '_frames_transition_in_progress', False))
            except Exception:
                try:
                    self._frames_transition_in_progress = False
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return None

    def remove_connection(self, connection):
        """Remove a connection and clean up references"""
        if connection in self.connections:
            # Remove from nodes' connection lists
            if hasattr(connection, 'start_node') and hasattr(connection, 'end_node'):
                if connection in connection.start_node.connections_out:
                    connection.start_node.connections_out.remove(connection)
                if connection in connection.end_node.connections_in:
                    connection.end_node.connections_in.remove(connection)
            # Remove from scene and list (use safe removal to avoid scene-mismatch warnings)
            if hasattr(self, 'safe_remove_item'):
                try:
                    self.safe_remove_item(connection)
                except Exception:
                    try:
                        self.removeItem(connection)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                try:
                    self.removeItem(connection)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.connections.remove(connection)
        try:
            self._frames_dirty = True
            self.schedule_frame_update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.refresh_mutex_connectors()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # schedule reflow in case any node became (dis)connected
        try:
            if getattr(self, 'auto_layout_enabled', False):
                if getattr(self, '_layout_in_progress', False):
                    pass
                else:
                    if not self._reflow_timer.isActive():
                        self._reflow_timer.start()
        except Exception:
            try:
                if getattr(self, 'auto_layout_enabled', False):
                    self.reflow_unconnected_nodes()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def remove_node(self, node: FocusNode):
        """Remove a node and all its connections"""
        if node.focus.id in self.nodes:
            try:
                if hasattr(self, '_spatial_index'):
                    self._spatial_index.remove(node)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if hasattr(self, '_visible_nodes_cache'):
                    self._visible_nodes_cache.discard(node)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Remove any cross-type links (notes, events)
            try:
                if hasattr(self, 'remove_links_for'):
                    self.remove_links_for(node)
                else:
                    if hasattr(self, 'remove_note_focus_links_for'):
                        self.remove_note_focus_links_for(node)
                    if hasattr(self, 'remove_event_focus_links_for'):
                        self.remove_event_focus_links_for(node)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Remove all connections
            for connection in node.connections_in[:]:
                self.remove_connection(connection)
            for connection in node.connections_out[:]:
                self.remove_connection(connection)
            # Remove node from scene and tracking (use safe removal)
            try:
                if hasattr(self, 'safe_remove_item'):
                    try:
                        self.safe_remove_item(node)
                    except Exception:
                        try:
                            self.removeItem(node)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                else:
                    try:
                        self.removeItem(node)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if node.focus.id in self.nodes:
                    del self.nodes[node.focus.id]
            except Exception:
                try:
                    del self.nodes[node.focus.id]
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Remove any mutex connectors attached to this node
            try:
                for key, mx in list(self.mutex_connectors.items()):
                    a_id, b_id = key
                    if a_id == node.focus.id or b_id == node.focus.id:
                            try:
                                if hasattr(self, 'safe_remove_item'):
                                    try:
                                        self.safe_remove_item(mx)
                                    except Exception:
                                        try:
                                            self.removeItem(mx)
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                else:
                                    try:
                                        self.removeItem(mx)
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            try:
                                del self.mutex_connectors[key]
                            except Exception:
                                try:
                                    del self.mutex_connectors[key]
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # update lineages and colors
            try:
                self.recompute_lineages()
                self.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if getattr(self, 'auto_layout_enabled', False):
                    if getattr(self, '_layout_in_progress', False):
                        pass
                    else:
                        if not self._reflow_timer.isActive():
                            if getattr(self, 'auto_layout_enabled', False):
                                self._reflow_timer.start()
            except Exception:
                try:
                    if getattr(self, 'auto_layout_enabled', False):
                        if getattr(self, 'auto_layout_enabled', False):
                            self.reflow_unconnected_nodes()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self._frames_dirty = True
                self.schedule_frame_update()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.refresh_mutex_connectors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def recompute_leaf_lineages(self) -> None:
        """Compute deepest descendant leaf for each node and assign a unique color per leaf."""
        with silent_operation("recompute_leaf_lineages"):
            children: Dict[str, List[str]] = {}
            for conn in self.connections:
                with silent_operation("collect_children"):
                    p = conn.start_node.focus.id
                    c = conn.end_node.focus.id
                    children.setdefault(p, []).append(c)
            nodes = list(self.nodes.keys())
            from functools import lru_cache

            @lru_cache(maxsize=None)
            def best_leaf(nid: str):
                ch = children.get(nid, [])
                if not ch:
                    return (0, nid)
                best = (-1, nid)
                for kid in ch:
                    d, leaf = best_leaf(kid)
                    if d + 1 > best[0]:
                        best = (d + 1, leaf)
                return best

            leaf_of: Dict[str, str] = {}
            for nid in nodes:
                leaf_of[nid] = best_leaf(nid)[1]
            self._leaf_of_node = leaf_of
            leaf_ids = sorted(set(leaf_of.values()))
            n = max(1, len(leaf_ids))
            self._leaf_colors.clear()
            for idx, lid in enumerate(leaf_ids):
                hue = (idx * (360.0 / n)) % 360.0
                col = QColor()
                col.setHslF(hue/360.0, 0.8, 0.45, 1.0)
                self._leaf_colors[lid] = col
            self._leaf_dirty = False

    def compact_connected_layout(self) -> None:
        """Pack connected focuses closer horizontally within each row while preserving y levels."""
        with silent_operation("compact_connected_layout"):
            # Respect global auto-layout toggle
            if not getattr(self, 'auto_layout_enabled', False):
                return
            # Avoid changing layout while user is creating connections
            if getattr(self, 'connection_mode', False):
                return
            if not self.nodes:
                return
            children: Dict[str, List[str]] = {}
            parents: Dict[str, List[str]] = {}
            for conn in self.connections:
                with silent_operation("collect_connection_data"):
                    p = conn.start_node.focus.id
                    c = conn.end_node.focus.id
                    children.setdefault(p, []).append(c)
                    parents.setdefault(c, []).append(p)
            levels: Dict[int, List[FocusNode]] = {}
            for node in self.nodes.values():
                if (node.focus.id in children) or (node.focus.id in parents):
                    levels.setdefault(int(node.focus.y), []).append(node)
            if not levels:
                return
            for _ in range(2):
                for y in sorted(levels.keys()):
                    row = levels[y]
                    if not row:
                        continue
                    items = []
                    for n in row:
                        ps = parents.get(n.focus.id, [])
                        if ps:
                            avg = sum(self.nodes[p].focus.x for p in ps if p in self.nodes) / max(1, len(ps))
                        else:
                            avg = n.focus.x
                        items.append((avg, n))
                    items.sort(key=lambda t: t[0])
                    if items:
                        avgx = sum(t[0] for t in items) / len(items)
                    else:
                        avgx = 0.0
                    start_x = int(round(avgx - (len(items)-1)/2.0))
                    cur_x = start_x
                    for _, n in items:
                        if n.focus.x != cur_x:
                            n.focus.x = cur_x
                            n.setPos(n.focus.x * GRID_UNIT, n.focus.y * GRID_UNIT)
                        cur_x += 1
            for node in self.nodes.values():
                node.update_connections()
            self._frames_dirty = True
            self.schedule_frame_update()

    def mousePressEvent(self, event):
        # Canonical canvas-only linking: when connection_mode is True, allow left-click
        # first node then left-click second node to create a link. If drag_to_link_mode
        # is enabled, show a rubber-banded line while moving the mouse.
        if self.connection_mode and event.button() == Qt.MouseButton.LeftButton:
            raw_item = self.itemAt(event.scenePos(), QTransform())
            # resolve child items up to a linkable node (FocusNode, EventNode, NoteNode)
            item = raw_item
            try:
                while item is not None and not isinstance(item, (FocusNode, EventNode, NoteNode)):
                    item = item.parentItem()
            except Exception:
                item = raw_item
            # Only support specific linkable item types
            if isinstance(item, (FocusNode, EventNode, NoteNode)):
                if self.connection_start is None:
                    # central helper to start a connection from this item
                    try:
                        self.start_connection_from(item, event.scenePos())
                    except Exception:
                        self.connection_start = item
                        try:
                            item.setSelected(True)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                else:
                    # Second click: complete the link if target differs
                    if item != self.connection_start:
                        try:
                            main = getattr(self, 'parent', None)
                            uw = getattr(main, 'undo_stack', None)
                            # Prefer undoable command for focus->focus prereq connections
                            if isinstance(self.connection_start, FocusNode) and isinstance(item, FocusNode):
                                if uw is not None:
                                    cmd = CreateConnectionCommand(self, self.connection_start.focus.id, item.focus.id)
                                    uw.push(cmd)
                                else:
                                    if self.connection_start.focus.id not in item.focus.prerequisites:
                                        item.focus.prerequisites.append(self.connection_start.focus.id)
                                    self.create_connection(self.connection_start.focus.id, item.focus.id)
                            else:
                                # Generic cross-type link (note/event/focus)
                                self.add_link(self.connection_start, item)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # cleanup rubber-band and selection
                    try:
                        if self._temp_link_item is not None:
                            try:
                                self.removeItem(self._temp_link_item)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            self._temp_link_item = None
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        if hasattr(self, 'parent') and getattr(self.parent, 'view', None) is not None:
                            try:
                                self.parent.view.setCursor(Qt.CursorShape.ArrowCursor)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.connection_start.setSelected(False)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.connection_start = None
            else:
                super().mousePressEvent(event)
        else:
            # Click on scene to clear lineage highlight if active and clicked empty space
            if not self.itemAt(event.scenePos(), QTransform()) and self._lineage_active:
                try:
                    self.clear_highlight()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Update rubber-banded temporary link when dragging in drag-to-link mode
        try:
            if self.connection_start is not None and self.drag_to_link_mode and self._temp_link_item is not None:
                start_pt = self.connection_start.scenePos()
                path = QPainterPath(start_pt)
                path.lineTo(event.scenePos())
                try:
                    self._temp_link_item.setPath(path)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            super().mouseMoveEvent(event)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def mouseReleaseEvent(self, event):
        # If drag-to-link temporal item exists but no connection was made, remove it
        try:
            if self._temp_link_item is not None:
                # If release occurred over a different node, mousePressEvent will handle linking.
                # Otherwise, clear the temporary path.
                target = self.itemAt(event.scenePos(), QTransform())
                if not isinstance(target, (FocusNode, EventNode, NoteNode)):
                    try:
                        self.removeItem(self._temp_link_item)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self._temp_link_item = None
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            super().mouseReleaseEvent(event)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def start_connection_from(self, item, scene_pos: Optional[QPointF] = None):
        """Begin a connection from `item`. Sets selection, optional rubber-band, and view cursor."""
        try:
            self.connection_start = item
            try:
                item.setSelected(True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # set view cursor to indicate linking
            try:
                if hasattr(self, 'parent') and getattr(self.parent, 'view', None) is not None:
                    try:
                        self.parent.view.setCursor(Qt.CursorShape.CrossCursor)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # create temporary rubber-band if configured
            if self.drag_to_link_mode:
                try:
                    start_pt = item.scenePos()
                    path = QPainterPath(start_pt)
                    end_pt = scene_pos if scene_pos is not None else start_pt
                    path.lineTo(end_pt)
                    self._temp_link_item = QGraphicsPathItem(path)
                    pen = QPen(QColor(100, 140, 240), max(1, int(getattr(self, 'connection_line_width', 2))))
                    pen.setStyle(Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    self._temp_link_item.setPen(pen)
                    try:
                        self._temp_link_item.setZValue(self.z_connections + 1)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.addItem(self._temp_link_item)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def cancel_connection_mode(self):
        """Cancel any active connection start and leave connection mode, restoring view state."""
        try:
            self.connection_mode = False
            # clear start
            try:
                if self.connection_start is not None:
                    try:
                        self.connection_start.setSelected(False)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.connection_start = None
            # remove temporary rubber-band
            try:
                if self._temp_link_item is not None:
                    try:
                        self.removeItem(self._temp_link_item)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self._temp_link_item = None
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # restore view cursor and drag mode
            try:
                if hasattr(self, 'parent') and getattr(self.parent, 'view', None) is not None:
                    try:
                        self.parent.view.setCursor(Qt.CursorShape.ArrowCursor)
                        self.parent.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # uncheck toolbar action if present
            try:
                if hasattr(self, 'parent') and hasattr(self.parent, 'connect_action'):
                    try:
                        self.parent.connect_action.setChecked(False)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # statusbar
            try:
                if hasattr(self, 'parent') and hasattr(self.parent, 'statusBar'):
                    try:
                        self.parent.statusBar().showMessage("Ready")
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # --------------
    # Placement/layout helpers
    # --------------
    def _node_degree(self, node: 'FocusNode') -> int:
        """Compute the degree (in+out) of a node based on current connection lists."""
        try:
            return len(node.connections_in) + len(node.connections_out)
        except Exception:
            return 0

    def reflow_unconnected_nodes(self) -> None:
        # Backwards-compatible wrapper: call the centralized layout coordinator
        try:
            if not getattr(self, 'auto_layout_enabled', False):
                return
            # Prevent re-entrancy: if a layout is already running, skip.
            if getattr(self, '_layout_in_progress', False):
                return
            self._layout_in_progress = True
            try:
                self.layout_focus_positions()
            finally:
                self._layout_in_progress = False
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # Centralized layout helpers
    def get_connected_and_isolated_nodes(self) -> tuple[list['FocusNode'], list['FocusNode']]:
        """Return (connected_nodes, isolated_nodes) based on current connections."""
        try:
            if not self.nodes:
                return ([], [])
            nodes_list = list(self.nodes.values())
            connected = [n for n in nodes_list if self._node_degree(n) > 0]
            isolated = [n for n in nodes_list if self._node_degree(n) == 0]
            return (connected, isolated)
        except Exception:
            return ([], [])

    def pack_isolated_nodes(self, connected: list['FocusNode'], isolated: list['FocusNode'], gap_x: int = 3) -> None:
        """Place isolated nodes in a stable right-side column adjacent to connected group."""
        try:
            if not isolated or not connected:
                return
            min_y = min(n.focus.y for n in connected)
            max_x = max(n.focus.x for n in connected)
            col_x = max_x + gap_x
            isolated.sort(key=lambda n: (n.focus.y, n.focus.id))
            for i, n in enumerate(isolated):
                new_x = col_x
                new_y = min_y + i
                if n.focus.x == new_x and n.focus.y == new_y:
                    continue
                n.focus.x = new_x
                n.focus.y = new_y
                n.setPos(n.focus.x * GRID_UNIT, n.focus.y * GRID_UNIT)
                try:
                    n.update_connections()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self._frames_dirty = True
                self.schedule_frame_update()
                self.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def layout_focus_positions(self, policy: str = 'balanced') -> None:
        """Central coordinator for focus layout.

        Policies:
        - 'balanced' (default): compact connected nodes, then pack isolated nodes.
        - 'connected-only': run only compact_connected_layout.
        - 'isolated-only': run only pack_isolated_nodes.
        """
        try:
            if not getattr(self, 'auto_layout_enabled', False):
                return
            if getattr(self, 'connection_mode', False):
                return
            if not self.nodes:
                return
            if policy == 'connected-only':
                if getattr(self, 'auto_layout_enabled', False):
                    self.compact_connected_layout()
                return
            # compute sets
            connected, isolated = self.get_connected_and_isolated_nodes()
            # compact connected group horizontally
            try:
                if getattr(self, 'auto_layout_enabled', False):
                    self.compact_connected_layout()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # pack isolated nodes if any
            try:
                if getattr(self, 'auto_layout_enabled', False) and isolated and connected:
                    self.pack_isolated_nodes(connected, isolated)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # --------------
    # Lineage helpers
    # --------------
    def _collect_lineage_ids(self, focus_id: str) -> set[str]:
        """Collect ancestors and descendants of the given focus id via prerequisites graph."""
        ids = set([focus_id])
        # maps
        parents_map: Dict[str, List[str]] = {}
        children_map: Dict[str, List[str]] = {}
        for nid, node in self.nodes.items():
            for p in node.focus.prerequisites:
                parents_map.setdefault(nid, []).append(p)
                children_map.setdefault(p, []).append(nid)
        # BFS up (ancestors)
        up = [focus_id]
        while up:
            cur = up.pop()
            for par in parents_map.get(cur, []):
                if par not in ids:
                    ids.add(par)
                    up.append(par)
        # BFS down (descendants)
        down = [focus_id]
        while down:
            cur = down.pop()
            for ch in children_map.get(cur, []):
                if ch not in ids:
                    ids.add(ch)
                    down.append(ch)
        return ids

    def highlight_lineage(self, focus_id: str) -> None:
        """Dim non-lineage nodes and connections, highlight the lineage."""
        try:
            ids = self._collect_lineage_ids(focus_id)
            self._lineage_active = True
            self._lineage_ids = ids

            # Build a typed adjacency for connectors so we can BFS from focus nodes and
            # include reachable EventNodes and NoteNodes in the lineage quick-view.
            visited = set()
            # Represent nodes as tuples (type, id) where type is 'F'|'E'|'N'
            q = []
            for fid in ids:
                key = ("F", fid)
                visited.add(key)
                q.append(key)

            # Build adjacency lists from connector lists
            adj = {}
            def add_edge(a, b):
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)

            try:
                for ef in list(getattr(self, '_event_focus_links', [])):
                    try:
                        eid = getattr(getattr(ef, 'event_node', None), 'event', None)
                        fid = getattr(getattr(ef, 'focus_node', None), 'focus', None)
                        if eid is not None and fid is not None:
                            a = ("E", getattr(eid, 'id', None))
                            b = ("F", getattr(fid, 'id', None))
                            add_edge(a, b)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for ee in list(getattr(self, '_event_event_links', [])):
                    try:
                        a_ev = getattr(getattr(ee, 'a', None), 'event', None)
                        b_ev = getattr(getattr(ee, 'b', None), 'event', None)
                        if a_ev is not None and b_ev is not None:
                            a = ("E", getattr(a_ev, 'id', None))
                            b = ("E", getattr(b_ev, 'id', None))
                            add_edge(a, b)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for nf in list(getattr(self, '_note_focus_links', [])):
                    try:
                        n = getattr(nf, 'note', None)
                        f = getattr(getattr(nf, 'focus_node', None), 'focus', None)
                        if n is not None and f is not None:
                            a = ("N", getattr(n, 'note_id', None))
                            b = ("F", getattr(f, 'id', None))
                            add_edge(a, b)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for ne in list(getattr(self, '_note_event_links', [])):
                    try:
                        n = getattr(ne, 'note', None)
                        e = getattr(getattr(ne, 'event_node', None), 'event', None)
                        if n is not None and e is not None:
                            a = ("N", getattr(n, 'note_id', None))
                            b = ("E", getattr(e, 'id', None))
                            add_edge(a, b)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # BFS expand visited set
            i = 0
            while i < len(q):
                cur = q[i]
                i += 1
                for nb in adj.get(cur, set()):
                    if nb not in visited:
                        visited.add(nb)
                        q.append(nb)

            # nodes: set opacities for focuses, events, and notes
            try:
                for fid, node in self.nodes.items():
                    key = ("F", fid)
                    node.setOpacity(1.0 if key in visited else 0.2)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                for eid, enode in getattr(self, 'event_nodes', {}).items():
                    key = ("E", eid)
                    enode.setOpacity(1.0 if key in visited else 0.2)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                for note in list(getattr(self, '_notes_items', []) or []):
                    key = ("N", getattr(note, 'note_id', None))
                    try:
                        note.setOpacity(1.0 if key in visited else 0.2)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # connections: treat connector visible if both endpoints are in visited set
            try:
                for conn in list(self.connections):
                    try:
                        sid = conn.start_node.focus.id
                        eid = conn.end_node.focus.id
                        on_path = (("F", sid) in visited) and (("F", eid) in visited)
                        conn.setOpacity(1.0 if on_path else 0.15)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # event/focus connectors
            try:
                for ef in list(getattr(self, '_event_focus_links', [])):
                    try:
                        e = getattr(getattr(ef, 'event_node', None), 'event', None)
                        f = getattr(getattr(ef, 'focus_node', None), 'focus', None)
                        on_path = False
                        if e is not None and f is not None:
                            on_path = (("E", getattr(e, 'id', None)) in visited) and (("F", getattr(f, 'id', None)) in visited)
                        ef.setOpacity(1.0 if on_path else 0.15)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                for ee in list(getattr(self, '_event_event_links', [])):
                    try:
                        a_ev = getattr(getattr(ee, 'a', None), 'event', None)
                        b_ev = getattr(getattr(ee, 'b', None), 'event', None)
                        on_path = False
                        if a_ev is not None and b_ev is not None:
                            on_path = (("E", getattr(a_ev, 'id', None)) in visited) and (("E", getattr(b_ev, 'id', None)) in visited)
                        ee.setOpacity(1.0 if on_path else 0.15)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                for nf in list(getattr(self, '_note_focus_links', [])):
                    try:
                        n = getattr(nf, 'note', None)
                        f = getattr(getattr(nf, 'focus_node', None), 'focus', None)
                        on_path = False
                        if n is not None and f is not None:
                            on_path = (("N", getattr(n, 'note_id', None)) in visited) and (("F", getattr(f, 'id', None)) in visited)
                        nf.setOpacity(1.0 if on_path else 0.15)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                for ne in list(getattr(self, '_note_event_links', [])):
                    try:
                        n = getattr(ne, 'note', None)
                        e = getattr(getattr(ne, 'event_node', None), 'event', None)
                        on_path = False
                        if n is not None and e is not None:
                            on_path = (("N", getattr(n, 'note_id', None)) in visited) and (("E", getattr(e, 'id', None)) in visited)
                        ne.setOpacity(1.0 if on_path else 0.15)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # frames dim
            for fr in self.frames:
                try:
                    fr.setOpacity(0.25)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def clear_highlight(self) -> None:
        """Restore full opacity to all items."""
        try:
            self._lineage_active = False
            self._lineage_ids.clear()
            for node in list(self.nodes.values()):
                try:
                    node.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for enode in list(getattr(self, 'event_nodes', {}).values()):
                try:
                    enode.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for note in list(getattr(self, '_notes_items', []) or []):
                try:
                    note.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for conn in list(self.connections):
                try:
                    conn.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for ef in list(getattr(self, '_event_focus_links', [])):
                try:
                    ef.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for ee in list(getattr(self, '_event_event_links', [])):
                try:
                    ee.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for nf in list(getattr(self, '_note_focus_links', [])):
                try:
                    nf.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for ne in list(getattr(self, '_note_event_links', [])):
                try:
                    ne.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for fr in self.frames:
                try:
                    fr.setOpacity(1.0)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def recompute_lineages(self) -> None:
        """Compute lineage groups based on primary-parent relation (parent at y-1)."""
        try:
            id_to_node = self.nodes
            # build primary parent map
            primary_parent: Dict[str, Optional[str]] = {}
            for nid, node in id_to_node.items():
                chosen = None
                for p in node.focus.prerequisites:
                    if p in id_to_node and int(round(id_to_node[p].focus.y)) == node.focus.y - 1:
                        chosen = p
                        break
                    if chosen is None:
                        chosen = p if p in id_to_node else None
                primary_parent[nid] = chosen
            # find roots and assign lineage id per node by walking up to root
            roots = set()
            for nid, par in primary_parent.items():
                if not par or par not in id_to_node:
                    roots.add(nid)
            # function to trace to root
            def root_of(x: str) -> str:
                seen = set()
                cur = x
                while True:
                    par = primary_parent.get(cur)
                    if not par or par in seen or par not in id_to_node:
                        return cur
                    seen.add(par)
                    cur = par
            lineage_of: Dict[str, str] = {}
            for nid in id_to_node.keys():
                lineage_of[nid] = root_of(nid)
            self._lineage_of_node = lineage_of
            # make colors per lineage root (distinct palette around hue wheel)
            root_ids = sorted(set(lineage_of.values()))
            n = max(1, len(root_ids))
            self._lineage_colors.clear()
            for idx, rid in enumerate(root_ids):
                hue = (idx * (360.0 / n)) % 360.0
                col = QColor()
                col.setHslF(hue/360.0, 0.75, 0.45, 1.0)
                self._lineage_colors[rid] = col
            # precompute lineage group rects for frames
            # Build rect per lineage id from node bounding rects
            lg: Dict[str, QRectF] = {}
            for nid, node in id_to_node.items():
                lid = self._lineage_of_node.get(nid)
                if lid is None:
                    continue
                nb = node.sceneBoundingRect()
                if lid not in lg:
                    lg[lid] = QRectF(nb)
                else:
                    lg[lid] = lg[lid].united(nb)
            self._precomputed_lineage_rects = lg
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

#endregion

#region Dialog Classes

#endregion

#region Application Window

class HOI4FocusTreeGenerator(QMainWindow):
    """Enhanced HOI4 Focus Tree Generator with Focus Library"""
    generation_finished = pyqtSignal(dict)
    generation_error = pyqtSignal(str)
    update_available_signal = pyqtSignal(object)  # emits updater
    no_update_signal = pyqtSignal()
    def __init__(self):
        super().__init__()
        # Connect update signals to UI handlers
        try:
            self.update_available_signal.connect(self.show_update_dialog)
            self.no_update_signal.connect(self.show_uptodate_dialog)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Keybindings manager (available to Settings dialog)
        try:
            self.keybinds = KeybindsManager(self)
        except Exception:
            self.keybinds = None
        self.focuses: List[Focus] = []
        self.events: List[Event] = []
        # Application version for internal tracking
        # Read from version.txt to avoid hardcoding and admin permission issues
        self.app_version = self._load_version_from_file() or "1.0.9"
        # path to the currently opened project file (if any)
        self.current_project_path: Optional[str] = None
        self.tree_id = "custom_focus_tree"
        self.country_tag = "TAG"
        # Library: key -> entry dict with folder support
        self.library: Dict[str, Dict[str, Any]] = {}
        # Folder metadata: folder_path -> {expanded: bool, color: str, etc}
        self.library_folders: Dict[str, Dict[str, Any]] = {}
        # Icon Library: name -> path (or identifier). Used for choosing icons for focuses
        self.icon_library: Dict[str, str] = {}
        # Optional convenient paths exposed to settings
        # app base dir (user-editable) and derived folders
        self.app_base_dir = None  # will be detected on startup
        self.icon_library_path = ''
        # Do not default projects_home_path to cwd (which may be the Python install
        # folder when the app is launched from a virtualenv). Leave it unset so
        # `ensure_app_dirs()` can pick the platform-appropriate AppData/XDG folder.
        self.projects_home_path = None
        # Logging controls
        self.logging_enabled = False

        # Initialization continued (these run as part of __init__ after the helper method)
        self.logging_level = 'INFO'
        # File logging handler (if enabled)
        self._file_log_handler = None
        # Cached app-wide canvas settings layered over project settings when preferred
        self._app_canvas_settings_cache: dict = {}
        # Optional naming theme loaded from JSON (category->list of strings)
        self.theme_data: Dict[str, List[str]] = {}
        self.theme_path: Optional[str] = None
        # Settings and database paths: defer to ensure_app_dirs so we can place them
        # under the platform-appropriate app base dir (AppData/XDG) by default.
        self.settings_path = None
        self.database_path = None
        # detect/create app base dir and required subfolders before UI
        try:
            self.detect_app_base_dir()
            self.ensure_app_dirs()
            # scan icons after creating/ensuring dirs
            try:
                self.scan_icon_library()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Undo stack for undo/redo of user actions
        try:
            self.undo_stack = QUndoStack(self)
        except Exception:
            self.undo_stack = None
        self.setup_ui()
        # Register keybindings and attach to window before loading settings
        try:
            if self.keybinds is not None:
                self.keybinds.set_owner(self)
                self._register_keybind_commands()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Load persisted settings and database now that UI is initialized
        try:
            self.load_settings()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.load_database()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # If no icon_library was loaded from the DB, scan the icon folder and persist
        try:
            if not getattr(self, 'icon_library', None):
                ip = getattr(self, 'icon_library_path', None)
                if ip and os.path.isdir(ip):
                    try:
                        self.scan_icon_library()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.save_database()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _load_version_from_file(self) -> Optional[str]:
        """Load application version from version.txt file in the workspace root.

        This ensures the correct version is displayed regardless of admin permissions.
        Falls back to None if file cannot be read.

        Returns:
            Version string (e.g., "1.0.9") or None if file cannot be read
        """
        try:
            # Try multiple potential locations for version.txt
            possible_paths = [
                os.path.join(os.path.dirname(__file__), 'version.txt'),
                os.path.join(os.getcwd(), 'version.txt'),
                'version.txt'
            ]

            for path in possible_paths:
                if os.path.exists(path):
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            version = f.read().strip()
                            if version:
                                logger.info(f"Loaded version {version} from {path}")
                                return version
                    except Exception as e:
                        logger.debug(f"Failed to read version from {path}: {e}")
                        continue
        except Exception as e:
            logger.debug(f"Exception in _load_version_from_file: {e}")

        return None

    def contextMenuEvent(self, event):
        """Suppress context menu events that occur over the main toolbar to
        prevent the toolbar from being deactivated via its context menu.
        """
        try:
            # If the event position is over the main toolbar, ignore it.
            pos = event.globalPos()
            try:
                from PyQt6.QtGui import QCursor
                widget = self.childAt(self.mapFromGlobal(pos))
            except Exception:
                widget = None
            # Walk up parents to see if it's inside a QToolBar
            w = widget
            while w is not None:
                try:
                    # compare class name to avoid importing QToolBar in many places
                    if w.__class__.__name__ == 'QToolBar' or getattr(w, 'objectName', lambda: '')() == getattr(self, '_main_toolbar', None).objectName() if getattr(self, '_main_toolbar', None) is not None else False:
                        # swallow the event
                        event.ignore()
                        return
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    w = w.parent()
                except Exception:
                    break
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # fallback to default behavior
        try:
            super().contextMenuEvent(event)
        except Exception:
            try:
                event.ignore()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Load settings early to restore previous state (including current project)
        try:
            self.load_settings()
            self.load_database()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _prefix_focus_id(self, fid: str) -> str:
        """Return a focus id prefixed with the current country tag if not already prefixed.
        Keeps existing IDs intact if they already contain the tag.
        """
        try:
            tag = (getattr(self, 'country_tag', None) or '').strip()
            if not tag:
                return fid
            tag = str(tag).upper()
            # If the id already starts with the tag (with common separators), leave it
            if fid.startswith(f"{tag}_") or fid.startswith(f"{tag}.") or fid.startswith(f"{tag}-") or fid == tag or fid.startswith(tag):
                return fid
            return f"{tag}_{fid}"
        except Exception:
            return fid

    def _sync_mutual_exclusive(self, focus_id: str) -> None:
        """Ensure mutual_exclusive relationships are symmetric for the given focus.

        - For each id listed in focus.mutually_exclusive, ensure the referenced focus
          also lists this focus's id.
        - Remove references to this focus from any other focus that are not present
          in this focus's list (keeps symmetry when removing exclusivity).
        - If the given focus_id does not exist, remove references to it from all focuses.
        """
        try:
            # find the focus by id
            target = next((f for f in getattr(self, 'focuses', []) if f.id == focus_id), None)
            if target is None:
                # remove stale references to missing id
                for f in getattr(self, 'focuses', []):
                    try:
                        if focus_id in getattr(f, 'mutually_exclusive', []):
                            f.mutually_exclusive = [m for m in f.mutually_exclusive if m != focus_id]
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    if hasattr(self, 'canvas'):
                        self.canvas.refresh_mutex_connectors()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return

            # Only enforce reciprocity for the given focus. Previously this
            # expanded connected components into cliques which produced
            # unintended transitive mutual exclusions (A-B and B-C => A-C).
            # Users expect mutually_exclusive to be a symmetric pairwise relation
            # (if A lists B then B lists A) but NOT transitive. So we:
            #  - Ensure target.mutually_exclusive contains only existing focus ids
            #  - For each id in target.mutually_exclusive ensure the referenced focus
            #    lists target.id as well (add if missing)
            #  - Remove target.id from any other focus that lists it but is not in
            #    target.mutually_exclusive (keep symmetry when the user removed an entry)
            try:
                id_to_focus = {f.id: f for f in getattr(self, 'focuses', []) if getattr(f, 'id', None) is not None}
                # filter target list to only existing ids and remove self-references
                try:
                    raw_list = getattr(target, 'mutually_exclusive', []) or []
                except Exception:
                    raw_list = []
                desired_set = set(x for x in raw_list if x in id_to_focus and x != getattr(target, 'id', None))

                # write back a cleaned, deterministic list on target
                try:
                    target.mutually_exclusive = sorted(desired_set)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Ensure reciprocity: for each other mentioned by target, ensure it mentions target
                for other_id in list(desired_set):
                    try:
                        other = id_to_focus.get(other_id)
                        if other is None:
                            continue
                        other_me = getattr(other, 'mutually_exclusive', []) or []
                        if getattr(target, 'id', None) not in other_me:
                            # append while preserving existing order as much as possible
                            try:
                                other.mutually_exclusive = list(other_me) + [target.id]
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Remove stale reciprocity: any focus that mentions target.id but is not in desired_set should have it removed
                for f in getattr(self, 'focuses', []):
                    try:
                        if getattr(f, 'id', None) == getattr(target, 'id', None):
                            continue
                        f_me = getattr(f, 'mutually_exclusive', []) or []
                        if getattr(target, 'id', None) in f_me and getattr(f, 'id', None) not in desired_set:
                            try:
                                f.mutually_exclusive = [m for m in f_me if m != target.id]
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                if hasattr(self, 'canvas'):
                    self.canvas.refresh_mutex_connectors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # ----- Keybindings integration -----
    def _first_selected_focus(self):
        try:
            sel = [i for i in self.canvas.selectedItems() if isinstance(i, FocusNode)]
            return sel[0].focus if sel else None
        except Exception:
            return None

    def _register_keybind_commands(self) -> None:
        if self.keybinds is None:
            return
        cmds: List[CommandSpec] = []
        def _infer_category_from_cid(cid: str) -> str:
            try:
                if cid.startswith('file.'):
                    return 'File'
                if cid.startswith('focus.'):
                    return 'Focus'
                if cid.startswith('view.') or cid.startswith('show.') or cid.startswith('arrange.'):
                    return 'View'
                if cid.startswith('conn.') or cid.startswith('link.') or cid.startswith('prereq.') or cid.startswith('notes.'):
                    return 'Canvas'
                if cid.startswith('library.') or cid.startswith('icon.') or cid.startswith('icon_library'):
                    return 'Library'
                if cid.startswith('gen.') or cid.startswith('generate'):
                    return 'Generate'
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return 'General'

        def add(cid, label, cb, default=None, category: Optional[str] = None):
            # Allow explicit category or infer from command id
            try:
                cat = category or _infer_category_from_cid(str(cid or ''))
            except Exception:
                cat = 'General'
            cmds.append(CommandSpec(cid=cid, label=label, callback=cb, default=default, category=cat))

    # File/Project
        add('file.new_project', 'New Project', self.new_project, 'Ctrl+Alt+N')
        add('file.load_project', 'Load Project', self.load_project, 'Ctrl+O')
        add('file.save_project', 'Save Project', self.save_project, 'Ctrl+S')
        add('file.quick_export', 'Quick Export to HOI4', self.export_focus_tree, None)
        add('file.export_panel', 'Open Export Panel', self.open_export_panel, 'Ctrl+Shift+E')
        add('file.generate', 'Generate Project', self.show_generate_dialog, 'Ctrl+G')
        add('file.new_node_palette', 'New Node…', self.open_node_palette, 'Ctrl+N')
        # Focus CRUD
        add('focus.add', 'Add Focus', self.add_focus, 'Ctrl+Shift+F')
        add('focus.edit', 'Edit First Selected Focus', lambda: self.edit_focus(self._first_selected_focus()), 'Enter')
        # Do not assign a default 'Delete' shortcut to avoid ambiguous shortcut warnings.
        add('focus.delete', 'Delete Selected Focuses', self.delete_selected_focuses, None)
        add('focus.duplicate', 'Duplicate First Selected Focus', lambda: self._first_selected_focus() and self.duplicate_focus(self._first_selected_focus()), 'Ctrl+D')
        add('focus.copy', 'Copy Selected Focuses', self.copy_selected_focuses, 'Ctrl+C')
        add('focus.paste', 'Paste Focuses', self.paste_focuses, 'Ctrl+V')
        add('focus.multi_add', 'Multi-Add Focuses', self.show_multi_add_dialog, 'Ctrl+M')
        add('focus.colorize', 'Colorize Selected Nodes', self.colorize_selected_nodes, 'Ctrl+K')
        # View / tools
        add('view.zoom_in', 'Zoom In', self.zoom_in, 'Ctrl++')
        add('view.zoom_out', 'Zoom Out', self.zoom_out, 'Ctrl+-')
        add('view.fit', 'Fit View', self.fit_view, 'Ctrl+0')
        add('view.toggle_frames', 'Toggle Frames', lambda: self.frames_action.trigger() if hasattr(self, 'frames_action') else None, None)
        add('view.toggle_grid', 'Toggle Grid', lambda: self.grid_action.trigger() if hasattr(self, 'grid_action') else None, None)
        add('view.toggle_lineage_colors', 'Toggle Lineage Coloring', lambda: self.lineage_color_action.trigger() if hasattr(self, 'lineage_color_action') else None, None)
        add('view.icon_view', 'Toggle Icon View Mode', lambda: self.icon_view_action.trigger() if hasattr(self, 'icon_view_action') else None, None)
        add('view.layer_manager', 'Open Layer Manager', lambda: QTimer.singleShot(0, lambda: LayerManagerDialog(self.canvas, parent=self).exec()), None)
        # Connection tools
        # Deprecated default shortcut for Connection Mode; now Ctrl+L is used for Link Selection
        add('conn.toggle_mode', 'Toggle Connection Mode', lambda: self.connect_action.trigger() if hasattr(self, 'connect_action') else None, None)
        # New: Link Selection smart linker as a first-class command with Ctrl+L
        add('link.selection', 'Link Selection', self.link_selected_chain_smart, 'Ctrl+L')
        # Prereq link mode quick commands (allow remapping in Keybinds editor)
        add('prereq.mode.normal', 'Set Prereq Mode: Exclusive/Direct', lambda: self._set_prereq_mode(None), 'Ctrl+E')
        add('prereq.mode.or', 'Set Prereq Mode: OR Group', lambda: self._set_prereq_mode('OR'), 'Ctrl+R')
        add('prereq.mode.and', 'Set Prereq Mode: AND Group', lambda: self._set_prereq_mode('AND'), 'Ctrl+A')
        add('arrange.vertical_selected', 'Arrange Selected Vertically', self.arrange_selected_vertically, 'Ctrl+Shift+V')
        add('arrange.vertical_all_by_root', 'Arrange All by Root Vertically', self.arrange_all_by_root_vertically, None)
        add('arrange.compact_connected', 'Compact Connected Layout', lambda: (self.canvas.compact_connected_layout() if getattr(self.canvas, 'auto_layout_enabled', False) else (self.statusBar().showMessage('Auto-layout disabled', 1500) if hasattr(self, 'statusBar') else None)), None)
        # Notes
        add('notes.toggle', 'Toggle Notes Mode', lambda: None, 'Ctrl+;')  # handled by menu toggle; editor tab can remap
        add('notes.add', 'Add Note', lambda: self.canvas.add_note("Note", self.view.mapToScene(self.view.mapFromGlobal(QCursor.pos())) if hasattr(self, 'view') else QPointF(0,0)), "Ctrl+'")
        add('notes.find', 'Find Notes', self.show_find_notes_dialog, 'Ctrl+F12')
        add('notes.clear', 'Clear All Notes', lambda: self.canvas.clear_notes(), None)
        # Append/grow
        add('gen.append', 'Append Generated Nodes…', self.show_append_dialog, 'Ctrl+Shift+G')
        # Network colors
        add('view.network_colors', 'Network Colors…', self.show_network_colors_dialog, None)
        # Icon library
        add('library.icon_library', 'Open Icon Library…', lambda: IconLibraryDialog(self.icon_library, parent=self).exec(), None)

        # Create overlay widget for quick keybind reference and register toggle command
        try:
            self._keybinds_overlay = KeybindsOverlay(self.keybinds, parent=self)
            add('show.keybinds_overlay', 'Toggle Keybinds Overlay', lambda: self._keybinds_overlay.toggle_overlay(), 'Ctrl+/')
        except Exception:
            # ensure variable exists
            self._keybinds_overlay = None

        self.keybinds.register_commands(cmds)
        # If State Viewport exists, include its command specs as well
        try:
            sv = getattr(self, 'state_viewport_dock', None)
            if sv and getattr(sv, 'get_command_specs', None):
                try:
                    extra = sv.get_command_specs() or []
                    if extra:
                        # Mark these commands as scoped to the state viewport so they only
                        # activate when the viewport is visible and focused.
                        try:
                            for s in extra:
                                try:
                                    setattr(s, 'widget_scope', 'state_viewport')
                                    # Ensure these commands are categorized under State Viewport
                                    if not getattr(s, 'category', None):
                                        try:
                                            setattr(s, 'category', 'State Viewport')
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # Register the viewport widget as a scope target
                        try:
                            self.keybinds.register_scope_widget('state_viewport', sv)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        self.keybinds.register_commands(extra)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def show_projects_home(self):
        """Open the Projects Home dialog. Prefer explicit projects_home_path, then app_base_dir/projects, then cwd."""
        start = getattr(self, 'projects_home_path', None)
        if not start:
            abd = getattr(self, 'app_base_dir', None)
            if abd:
                start = os.path.join(abd, 'projects')
        if not start:
            start = os.getcwd()
        dialog = ProjectsHomeDialog(start_dir=start, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.selected_path:
                # Remember the folder we loaded from for next time
                try:
                    self.projects_home_path = os.path.dirname(dialog.selected_path)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.load_project_from_path(dialog.selected_path)

    def new_project(self):
        """Create a new project file in the projects home folder and load it."""
        try:
            # Use the same folder resolution as Projects dialog and Save Project
            start = getattr(self, 'projects_home_path', None)
            if not start:
                abd = getattr(self, 'app_base_dir', None)
                if abd:
                    start = os.path.join(abd, 'projects')
            if not start:
                start = os.getcwd()
            name, ok = QInputDialog.getText(self, "New Project", "Enter new project filename (without extension):")
            if not ok or not name.strip():
                return
            fn = os.path.join(start, f"{name.strip()}.json")
            base = {'version': getattr(self, 'app_version', '1.0.9'), 'tree_id': name.strip(), 'country_tag': 'TAG', 'focuses': [], 'library': {}}
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, 'w', encoding='utf-8') as f:
                json.dump(base, f, indent=2)
            # Load the new project
            self.load_project_from_path(fn)
        except Exception as e:
            show_error(self, "New Project", "Failed to create project.", exc=e)

    def load_project_from_path(self, path: str):
        """Load a project file by path and set current_project_path on success."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                project = json.load(f)
            # basic validation to avoid loading arbitrary JSON
            if not isinstance(project, dict) or not isinstance(project.get('focuses', None), list):
                raise ValueError("Invalid project file: missing 'focuses' list")
            self.load_project_from_dict(project)
            self.current_project_path = path
            self.setWindowTitle(f"HOI4 Focus GUI - {os.path.basename(path)}")
            QMessageBox.information(self, "Loaded", f"Project loaded from {obfuscate_path(path)}")
        except Exception as e:
            show_error(self, "Error", "Failed to load project.", exc=e)
        # Only load settings/database if this is a manual load (not during startup restoration)
        # Check if we're in startup by seeing if the canvas has any nodes yet
        if hasattr(self, 'canvas') and len(getattr(self.canvas, 'nodes', {})) > 0:
            # This is a manual load of a different project, so save current state
            try:
                self.save_settings()  # Save the new current_project_path
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # ensure reliable refresh after loading (addresses disappearing elements)
        try:
            self.raise_()
            try:
                self.activateWindow()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.view.viewport().update()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            QTimer.singleShot(0, self.fit_view)
            try:
                # Give the event loop a moment and then re-apply connection styling
                restyle = lambda: (
                    getattr(getattr(self, 'canvas', None), 'rebuild_connection_styles', None)
                    or getattr(getattr(self, 'canvas', None), '_apply_prereq_group_styles', None)
                    or getattr(self, '_apply_prereq_group_styles', lambda: None)
                )()
                QTimer.singleShot(50, restyle)
                try:
                    QTimer.singleShot(250, restyle)
                    QTimer.singleShot(1000, restyle)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # ---- HOI4 pill image auto-detect / prompt ----
    def _steam_library_candidates(self) -> list:
        cands = []
        try:
            pf86 = os.environ.get('PROGRAMFILES(X86)') or r"C:\\Program Files (x86)"
            pf = os.environ.get('PROGRAMFILES') or r"C:\\Program Files"
            for base in (pf86, pf):
                p = os.path.join(base, 'Steam', 'steamapps')
                if os.path.isdir(p):
                    cands.append(p)
            alt = r"C:\\SteamLibrary\\steamapps"
            if os.path.isdir(alt):
                cands.append(alt)
            # libraryfolders.vdf for extra libraries
            vdf = os.path.join(pf86, 'Steam', 'steamapps', 'libraryfolders.vdf')
            if os.path.isfile(vdf):
                try:
                    with open(vdf, 'r', encoding='utf-8', errors='ignore') as f:
                        txt = f.read()
                    import re
                    for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
                        path = m.group(1)
                        app = os.path.join(path, 'steamapps')
                        if os.path.isdir(app):
                            cands.append(app)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # dedupe preserve order
        out = []
        seen = set()
        for c in cands:
            if c not in seen:
                out.append(c); seen.add(c)
        return out

    def detect_hoi4_pill_image(self) -> Optional[str]:
        rel = os.path.join('Hearts of Iron IV', 'gfx', 'interface', 'focusview', 'titlebar', 'focus_can_start_bg.dds')
        try:
            for libs in self._steam_library_candidates():
                cand = os.path.join(libs, 'common', rel)
                if os.path.isfile(cand):
                    return cand
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return None

    def locate_hoi4_pill_image(self, prompt_if_missing: bool = True) -> Optional[str]:
        path = self.detect_hoi4_pill_image()
        if path and os.path.isfile(path):
            return path
        if not prompt_if_missing:
            return None
        # prompt user for exact file
        hint = ''
        try:
            libs = self._steam_library_candidates()
            if libs:
                hint = os.path.join(libs[0], 'common', 'Hearts of Iron IV', 'gfx', 'interface', 'focusview', 'titlebar')
        except Exception:
            hint = ''
        fn, _ = QFileDialog.getOpenFileName(self, "Locate focus_can_start_bg.dds", hint, "DDS Image (focus_can_start_bg.dds);;DDS (*.dds);;All Files (*.*)")
        if not fn:
            return None
        if os.path.basename(fn).lower() != 'focus_can_start_bg.dds':
            QMessageBox.warning(self, "Invalid File", "Please select the exact file 'focus_can_start_bg.dds'.")
            return None
        if not os.path.isfile(fn):
            QMessageBox.warning(self, "Missing File", "The selected file does not exist.")
            return None
        return fn

    def _setup_default_pill_image(self) -> None:
        try:
            if getattr(self.canvas, 'title_pill_image_path', ''):
                return
            path = self.locate_hoi4_pill_image(prompt_if_missing=False)
            if not path:
                return
            self.canvas.title_pill_image_path = path
            try:
                self.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def setup_ui(self):
        self.setWindowTitle("HOI4 Focus GUI")
        self.setGeometry(100, 100, 1400, 900)

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Canvas/view must exist before menus bind helpers that access them
        if getattr(self, 'canvas', None) is None:
            self.canvas = FocusTreeCanvas(self)
        if getattr(self, 'view', None) is None:
            self.view = EnhancedGraphicsView(self.canvas)

        # Enhanced toolbar (condensed into dropdown menus)
        toolbar = QToolBar()
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        # Prevent users from deactivating the toolbar via its own context menu
        try:
            toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        except Exception:
            try:
                # fallback for older PyQt variants
                toolbar.setContextMenuPolicy(Qt.NoContextMenu)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Make toolbar fixed in place and only on the top area
        try:
            toolbar.setMovable(False)
            toolbar.setFloatable(False)
            self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        except Exception:
            self.addToolBar(toolbar)
        # Also record for any other logic that checks main toolbar
        try:
            toolbar.setObjectName('_MainToolbar')
            self._main_toolbar = toolbar
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Mute action (app-level) — visible in top toolbar
        mute_action = QAction("Mute App", self)
        # keep a reference so load_settings can update the toolbar state
        try:
            self.mute_action = mute_action
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # GUI Editor quarantined: action removed from main app toolbar
        # (Previously added a toolbar action that imported _gui_editor.GuiEditorWindow and opened it.)
        mute_action.setCheckable(True)
        try:
            mute_action.setChecked(bool(getattr(self, 'muted', False)))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        def _toggle_mute(checked):
            try:
                self.muted = bool(checked)
                # Also silence logger handlers if muting console logging
                if getattr(self, 'logging_enabled', False) and not getattr(self, 'muted', False):
                    # keep logging as-is
                    pass
                # Persist immediate app preference if desired
                try:
                    if hasattr(self, 'save_settings'):
                        self.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        mute_action.toggled.connect(_toggle_mute)
        toolbar.addAction(mute_action)

        # Persistent Save indicator (dim and bright icons for clear feedback)
        try:
            # bright green
            pm_bright = QPixmap(16, 16)
            pm_bright.fill(QColor('#44aa44'))
            ico_bright = QIcon(pm_bright)
            # dim gray
            pm_dim = QPixmap(16, 16)
            pm_dim.fill(QColor('#7a7a7a'))
            ico_dim = QIcon(pm_dim)
            self._save_icon_bright = ico_bright
            self._save_icon_dim = ico_dim
            self._save_indicator_action = QAction(self._save_icon_dim, 'Saved', self)
            self._save_indicator_action.setToolTip('Indicates recent save activity')
            toolbar.addAction(self._save_indicator_action)
            # keep a reference to the main toolbar for potential direct widget styling
            try:
                self._main_toolbar = toolbar
            except Exception:
                self._main_toolbar = None
        except Exception:
            self._save_indicator_action = None
            self._save_icon_bright = None
            self._save_icon_dim = None

        # Create QAction objects (wired up) but add them into menus below
        add_action = QAction("Add Focus", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        add_action.triggered.connect(self.add_focus)

        connect_action = QAction("Connection Mode", self)
        connect_action.setCheckable(True)
        connect_action.triggered.connect(self.toggle_connection_mode)
        self.connect_action = connect_action

        # Prerequisite link mode submenu (Normal / OR / AND) - will be added to Tools menu
        try:
            from PyQt6.QtGui import QActionGroup
            self._prereq_mode_group = QActionGroup(self)
            # Create a dedicated submenu that will be inserted into Tools
            self.prereq_submenu = QMenu("Prereq Link Mode", self)

            self.prereq_mode_normal = QAction("Normal", self)
            self.prereq_mode_normal.setCheckable(True)
            self.prereq_mode_normal.setChecked(True)
            self.prereq_mode_normal.toggled.connect(lambda chk: self._set_prereq_mode(None if chk else None))
            self._prereq_mode_group.addAction(self.prereq_mode_normal)
            self.prereq_submenu.addAction(self.prereq_mode_normal)

            self.prereq_mode_or = QAction("OR Group", self)
            self.prereq_mode_or.setCheckable(True)
            self.prereq_mode_or.toggled.connect(lambda chk: self._set_prereq_mode('OR' if chk else None))
            self._prereq_mode_group.addAction(self.prereq_mode_or)
            self.prereq_submenu.addAction(self.prereq_mode_or)

            self.prereq_mode_and = QAction("AND Group", self)
            self.prereq_mode_and.setCheckable(True)
            self.prereq_mode_and.toggled.connect(lambda chk: self._set_prereq_mode('AND' if chk else None))
            self._prereq_mode_group.addAction(self.prereq_mode_and)
            self.prereq_submenu.addAction(self.prereq_mode_and)
        except Exception:
            self._prereq_mode_group = None

        gen_action = QAction("Generate Project", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        gen_action.triggered.connect(self.show_generate_dialog)

        export_action = QAction("Quick Export to HOI4", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        export_action.triggered.connect(self.export_focus_tree)
        export_panel_action = QAction("Export…", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        export_panel_action.triggered.connect(self.open_export_panel)

        save_action = QAction("Save Project", self)
        # Shortcut handled by keybinds manager to avoid ambiguous shortcut warnings
        save_action.triggered.connect(self.save_project_and_settings)

        load_action = QAction("Load Project", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        load_action.triggered.connect(self.load_project)

        # New Project / Projects Home
        new_project_action = QAction("New Project", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        new_project_action.triggered.connect(self.new_project)

        projects_home_action = QAction("Projects Home", self)
        projects_home_action.triggered.connect(self.show_projects_home)

        # Library actions
        save_lib_action = QAction("Save Library...", self)
        save_lib_action.triggered.connect(self.save_library_to_file)

        load_lib_action = QAction("Load Library...", self)
        load_lib_action.triggered.connect(self.load_library_from_file)

        # Icon Library action
        icon_lib_action = QAction("Icon Library...", self)
        def _open_icon_library():
            try:
                dlg = IconLibraryDialog(self.icon_library, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    # If an icon was selected, apply to currently selected focuses
                    chosen = getattr(dlg, 'selected', None)
                    if chosen:
                        nodes = [it for it in self.canvas.selectedItems() if isinstance(it, FocusNode)]
                        if nodes:
                            for node in nodes:
                                try:
                                    node.focus.icon = str(chosen)
                                    # clear any cached pixmap on the focus
                                    if hasattr(node.focus, '_cached_icon_pixmap'):
                                        delattr(node.focus, '_cached_icon_pixmap')
                                    node.update()
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # persist icon library after changes
                        self.save_database()
            except Exception as e:
                show_error(self, "Icon Library", f"Failed to open icon library: {obfuscate_text(str(e))}", exc=e)
        icon_lib_action.triggered.connect(_open_icon_library)

        # View / tools actions
        zoom_in_action = QAction("Zoom In", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        zoom_in_action.triggered.connect(self.zoom_in)

        zoom_out_action = QAction("Zoom Out", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        zoom_out_action.triggered.connect(self.zoom_out)

        fit_action = QAction("Fit View", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        fit_action.triggered.connect(self.fit_view)

        # Copy / Paste actions
        copy_action = QAction("Copy", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        copy_action.triggered.connect(self.copy_selected_focuses)

        paste_action = QAction("Paste", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        paste_action.triggered.connect(self.paste_focuses)

        # Multi-add action
        multi_add_action = QAction("Multi-Add", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        multi_add_action.triggered.connect(self.show_multi_add_dialog)

        # Colorize selected nodes
        colorize_action = QAction("Colorize Selected", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        colorize_action.triggered.connect(self.colorize_selected_nodes)

        # Notes actions
        notes_toggle_action = QAction("Notes Mode", self)
        notes_toggle_action.setCheckable(True)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        def _toggle_notes(chk):
            try:
                self.canvas.notes_enabled = bool(chk)
                # update visibility of existing notes
                for it in getattr(self.canvas, '_notes_items', []):
                    try:
                        it.setVisible(self.canvas.notes_enabled)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # toggle visibility of note-related connectors to match notes mode
                try:
                    for nf in list(getattr(self.canvas, '_note_focus_links', [])):
                        nf.setVisible(self.canvas.notes_enabled)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    for ne in list(getattr(self.canvas, '_note_event_links', [])):
                        ne.setVisible(self.canvas.notes_enabled)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # event↔focus links are independent of notes; ensure they stay visible
                try:
                    for ef in list(getattr(self.canvas, '_event_focus_links', [])):
                        ef.setVisible(True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        notes_toggle_action.toggled.connect(_toggle_notes)
        # expose for keybindings
        self.notes_toggle_action = notes_toggle_action

        add_note_action = QAction("Add Note", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        def _add_note():
            try:
                if not getattr(self.canvas, 'notes_enabled', False):
                    self.canvas.notes_enabled = True
                    notes_toggle_action.setChecked(True)
                # paste at mouse position
                pos = QPointF(0, 0)
                try:
                    pos = self.view.mapToScene(self.view.mapFromGlobal(QCursor.pos()))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.canvas.add_note("Note", pos)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        add_note_action.triggered.connect(_add_note)

        clear_notes_action = QAction("Clear Notes", self)
        clear_notes_action.triggered.connect(lambda: getattr(self.canvas, 'clear_notes', lambda: None)())

        # Delete selected notes (batch)
        del_notes_action = QAction("Delete Selected Notes", self)
        def _del_selected_notes():
            try:
                items = [it for it in self.canvas.selectedItems() if isinstance(it, NoteNode)]
                if not items:
                    QMessageBox.information(self, "Notes", "No notes selected.")
                    return
                for it in list(items):
                    try:
                        it._delete_self()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        del_notes_action.triggered.connect(_del_selected_notes)

        # Find Notes action
        find_notes_action = QAction("Find Notes…", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        find_notes_action.triggered.connect(self.show_find_notes_dialog)

        # Connect selected notes (now uses unified smart linker)
        connect_notes_action = QAction("Connect Selected Notes", self)
        def _connect_notes():
            try:
                self.link_selected_chain_smart()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        connect_notes_action.triggered.connect(_connect_notes)

        # Project Note Settings (defaults)
        note_settings_action = QAction("Project Note Settings…", self)
        def _open_note_settings():
            try:
                dlg = ProjectNoteSettingsDialog(self.canvas, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    # apply defaults to existing notes
                    d = getattr(self.canvas, 'note_defaults', {})
                    for it in getattr(self.canvas, '_notes_items', []):
                        try:
                            it.apply_defaults(d)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        note_settings_action.triggered.connect(_open_note_settings)

        # Link a selected Note to a selected Focus
        link_note_focus_action = QAction("Link Note → Focus", self)
        def _link_note_focus():
            try:
                items = list(self.canvas.selectedItems())
                note = next((it for it in items if isinstance(it, NoteNode)), None)
                node = next((it for it in items if isinstance(it, FocusNode)), None)
                if not note or not node:
                    QMessageBox.information(self, "Notes", "Select one Note and one Focus to link.")
                    return
                self.canvas.add_note_focus_link(note, node)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        link_note_focus_action.triggered.connect(_link_note_focus)

        # Unlink Note↔Focus for selected Note or Focus
        unlink_note_focus_action = QAction("Unlink Note ↔ Focus", self)
        def _unlink_note_focus():
            try:
                items = list(self.canvas.selectedItems())
                target = next((it for it in items if isinstance(it, NoteNode) or isinstance(it, FocusNode)), None)
                if not target:
                    QMessageBox.information(self, "Notes", "Select a Note or Focus that has links.")
                    return
                self.canvas.remove_note_focus_links_for(target)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        unlink_note_focus_action.triggered.connect(_unlink_note_focus)

        # Link a selected Note to a selected Event
        link_note_event_action = QAction("Link Note → Event", self)
        def _link_note_event():
            try:
                items = list(self.canvas.selectedItems())
                note = next((it for it in items if isinstance(it, NoteNode)), None)
                evn = next((it for it in items if isinstance(it, EventNode)), None)
                if not note or not evn:
                    QMessageBox.information(self, "Notes", "Select one Note and one Event to link.")
                    return
                self.canvas.add_note_event_link(note, evn)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        link_note_event_action.triggered.connect(_link_note_event)

        unlink_note_event_action = QAction("Unlink Note ↔ Event", self)
        def _unlink_note_event():
            try:
                items = list(self.canvas.selectedItems())
                target = next((it for it in items if isinstance(it, NoteNode) or isinstance(it, EventNode)), None)
                if not target:
                    QMessageBox.information(self, "Notes", "Select a Note or Event that has links.")
                    return
                self.canvas.remove_note_event_links_for(target)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        unlink_note_event_action.triggered.connect(_unlink_note_event)

        net_color_action = QAction("Network Colors...", self)
        net_color_action.triggered.connect(self.show_network_colors_dialog)

        # Generic Link Selection (now calls unified smart linker)
        link_selection_action = QAction("Link Selection", self)
        link_selection_action.triggered.connect(lambda: self.link_selected_chain_smart())

        # Event-specific links
        link_event_focus_action = QAction("Link Event ↔ Focus", self)
        link_event_focus_action.triggered.connect(lambda: self.link_selected_chain_smart())

        unlink_event_focus_action = QAction("Unlink Event ↔ Focus", self)
        def _unlink_event_focus():
            try:
                items = list(self.canvas.selectedItems())
                target = next((it for it in items if isinstance(it, EventNode) or isinstance(it, FocusNode)), None)
                if not target:
                    QMessageBox.information(self, "Unlink", "Select an Event or Focus with links.")
                    return
                self.canvas.remove_event_focus_links_for(target)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        unlink_event_focus_action.triggered.connect(_unlink_event_focus)

        link_event_event_action = QAction("Link Event ↔ Event", self)
        link_event_event_action.triggered.connect(lambda: self.link_selected_chain_smart())

        unlink_event_event_action = QAction("Unlink Event ↔ Event", self)
        def _unlink_event_event():
            try:
                items = list(self.canvas.selectedItems())
                target = next((it for it in items if isinstance(it, EventNode)), None)
                if not target:
                    QMessageBox.information(self, "Unlink", "Select an Event with links.")
                    return
                self.canvas.remove_event_event_links_for(target)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        unlink_event_event_action.triggered.connect(_unlink_event_event)
        # Show/hide grid
        grid_action = QAction("Show Grid", self)
        grid_action.setCheckable(True)
        grid_action.setChecked(True)
        def _toggle_grid(chk):
            try:
                self.canvas.set_grid_visible(bool(chk))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        grid_action.toggled.connect(_toggle_grid)
        self.grid_action = grid_action

        frames_action = QAction("Show Frames", self)
        frames_action.setCheckable(True)
        frames_action.setChecked(True)
        frames_action.triggered.connect(self.toggle_frames)
        self.frames_action = frames_action

        lineage_color_action = QAction("Color Lines by Lineage", self)
        lineage_color_action.setCheckable(True)
        lineage_color_action.setChecked(True)
        def _toggle_line_colors(chk):
            try:
                self.canvas.color_lines_by_lineage = bool(chk)
                self.canvas.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        lineage_color_action.toggled.connect(_toggle_line_colors)
        self.lineage_color_action = lineage_color_action

        # Build menus and add them to the toolbar as dropdowns
        file_menu = QMenu("File", self)
        file_menu.addAction(new_project_action)
        # Node palette: shortcut handled by KeybindsManager (Ctrl+N); QAction has no shortcut to avoid conflicts
        node_palette_action = QAction("New Node…", self)
        node_palette_action.triggered.connect(self.open_node_palette)
        file_menu.addAction(node_palette_action)
        # Event creation
        add_event_action = QAction("Add Event", self)
        def _add_event():
            try:
                # create a default event at view center
                eid_base = f"{self.tree_id}.event"
                eid = eid_base
                i = 1
                existing = {e.id for e in getattr(self, 'events', [])}
                while eid in existing:
                    eid = f"{eid_base}.{i}"; i += 1
                # position at current mouse or center
                pos = QPointF(0, 0)
                try:
                    pos = self.view.mapToScene(self.view.mapFromGlobal(QCursor.pos()))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                ex = int(round(pos.x() / GRID_UNIT)); ey = int(round(pos.y() / GRID_UNIT))
                ev = Event(id=eid, title="New Event", description="", x=ex, y=ey)
                # Provide a helpful placeholder for trigger and options so editor isn't empty
                try:
                    pid = str(eid)
                    ev.trigger = f"trigger = {{\n\t# conditions for {pid}\n}}"
                except Exception:
                    ev.trigger = "trigger = {\n\t# conditions\n}"
                try:
                    pid = str(eid)
                    ev.options_block = (f"option = {{\n\tname = {pid}.a\n\t# add effects here\n}}\n\n"
                                        f"# Add additional options as needed\n")
                except Exception:
                    ev.options_block = "option = { name = event.1.a }"
                self.events.append(ev)
                self.canvas.add_event_node(ev)
                # open editor immediately
                self.edit_event(ev)
            except Exception as e:
                show_error(self, "Event", "Failed to add event.", exc=e)
        add_event_action.triggered.connect(_add_event)
        # 'Add Event' action intentionally omitted from File menu to declutter toolbar dropdown
        file_menu.addSeparator()
        file_menu.addAction(save_action)
        file_menu.addAction(load_action)
        # Import HOI4 focus .txt (lossless parser -> project JSON)
        import_txt_action = QAction("Import Focus (.txt)...", self)
        def _import_txt():
            try:
                # Default: do not cull unknown blocks unless the user explicitly requests it
                cull_unknown = False
                if convert_txt_to_project_dict is None:
                    QMessageBox.warning(self, "Import Unavailable", "Text import converter is not available.")
                    return
                start_dir = getattr(self, 'projects_home_path', '') or ''
                filename, _ = QFileDialog.getOpenFileName(self, "Import Focus (.txt)", start_dir, "Text Files (*.txt);;All Files (*)")
                if not filename:
                    return
                # Work on a temporary copy to avoid modifying the original file
                import tempfile, shutil
                temp_path = None
                try:
                    temp_dir = tempfile.mkdtemp(prefix='hoi4_import_')
                    temp_path = os.path.join(temp_dir, os.path.basename(filename))
                    try:
                        shutil.copy2(filename, temp_path)
                    except Exception:
                        # If copy fails, fall back to reading original but warn
                        temp_path = filename
                    with open(temp_path, 'r', encoding='utf-8') as f:
                        txt = f.read()
                except Exception as e:
                    show_error(self, "Read Error", f"Failed to read {obfuscate_path(filename)} (temp copy)", exc=e)
                    return

                # EXPERIMENTAL warning: parsing/import may not be lossless.
                try:
                    if not getattr(self, '_suppress_import_warning', False):
                        dlg = QDialog(self)
                        dlg.setWindowTitle('Experimental Import')
                        lay = QVBoxLayout(dlg)
                        lab = QLabel('EXPERIMENTAL: Importing HOI4 focus .txt is experimental.\n\nPlease BACK UP your focus files before proceeding.\nAfter import, carefully verify prerequisites and exports in-game.')
                        lab.setWordWrap(True)
                        lay.addWidget(lab)
                        cb = QCheckBox("Don't show this again")
                        lay.addWidget(cb)
                        # Option: Cull unrecognized brace-blocks from imported focuses
                        cull_cb = QCheckBox("Cull unknown/unparsed blocks (safe, non-destructive)")
                        cull_cb.setChecked(False)
                        lay.addWidget(cull_cb)
                        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                        lay.addWidget(btns)
                        def _on_btn(b):
                            try:
                                if b == QDialogButtonBox.StandardButton.Ok:
                                    dlg.accept()
                                else:
                                    dlg.reject()
                            except Exception:
                                try:
                                    dlg.reject()
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        btns.clicked.connect(lambda bt: _on_btn(btns.standardButton(bt)))
                        if dlg.exec() != QDialog.DialogCode.Accepted:
                            return
                        if cb.isChecked():
                            try:
                                setattr(self, '_suppress_import_warning', True)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            cull_unknown = bool(cull_cb.isChecked())
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                try:
                    parsed = convert_txt_to_project_dict(txt)
                except Exception as e:
                    show_error(self, "Parse Error", f"Failed to parse focus .txt file: {obfuscate_path(filename)}", exc=e)
                    return

                # Build a project dict compatible with load_project_from_dict
                project: dict = {
                    'version': 'imported_txt_v1',
                    'tree_id': parsed.get('tree_info', {}).get('id') or os.path.splitext(os.path.basename(filename))[0],
                    'country_tag': getattr(self, 'country_tag', 'TAG'),
                    'focuses': [],
                    'library': getattr(self, 'library', {}) or {}
                }

                # Collect parsed focuses - parser has already resolved all relative positions
                import_focuses = []
                for fx in parsed.get('focuses', []):
                    # Parser has already resolved relative_position_id to absolute coordinates
                    # Just extract the values directly
                    fid = fx.get('id') or fx.get('name') or ''
                    name = fx.get('name') or fx.get('text') or fid

                    try:
                        cost = int(fx.get('cost') or 10)
                    except:
                        cost = 10

                    # These are ABSOLUTE coordinates (parser already resolved relative positions)
                    try:
                        x_val = int(fx.get('x', 0))
                    except:
                        x_val = 0

                    try:
                        y_val = int(fx.get('y', 0))
                    except:
                        y_val = 0

                    # Prefer the cleaned raw version if the user requested culling
                    if cull_unknown and fx.get('has_unparsed'):
                        description = fx.get('description') or fx.get('clean_raw') or ''
                    else:
                        description = fx.get('description') or fx.get('raw') or ''

                    # Prerequisites: parser returns list-of-lists for AND groups
                    prereqs = fx.get('prerequisites', [])
                    prereq_groups = fx.get('prerequisites_groups', [])

                    # Build flattened list for compatibility
                    flat_pr = []
                    if isinstance(prereqs, list):
                        for item in prereqs:
                            if isinstance(item, list):
                                flat_pr.extend(p for p in item if p and p not in flat_pr)
                            elif item and item not in flat_pr:
                                flat_pr.append(item)

                    # Build grouped structure for rendering. Parser returns
                    # list-of-lists where each inner list represents an OR group
                    # (i.e. focus = A focus = B inside one prerequisite block means A OR B).
                    # The outer list represents AND between groups. Preserve explicit
                    # group dicts when provided. Singleton groups (one item) are
                    # flattened into the flat prerequisites list so they render as a
                    # single prereq connection line.
                    prerequisites_groups = []
                    if prereq_groups:
                        for grp in prereq_groups:
                            # If parser gave a dict-like group (rare), preserve its type
                            if isinstance(grp, dict):
                                try:
                                    gtype = (grp.get('type') or 'OR').upper()
                                except Exception:
                                    gtype = 'OR'
                                items = [str(x) for x in (grp.get('items') or []) if x]
                                if not items:
                                    continue
                                if len(items) == 1:
                                    if items[0] not in flat_pr:
                                        flat_pr.append(items[0])
                                    continue
                                prerequisites_groups.append({'type': gtype, 'items': items})
                            elif isinstance(grp, list):
                                items = [str(x) for x in grp if x]
                                if not items:
                                    continue
                                # inner lists from parser are OR groups
                                if len(items) == 1:
                                    if items[0] not in flat_pr:
                                        flat_pr.append(items[0])
                                    continue
                                prerequisites_groups.append({'type': 'OR', 'items': items})
                            else:
                                # unknown format: coerce to single item if possible
                                try:
                                    val = str(grp)
                                    if val and val not in flat_pr:
                                        flat_pr.append(val)
                                except Exception:
                                    continue
                    elif prereqs and prereqs and isinstance(prereqs[0], list):
                        for grp in prereqs:
                            if isinstance(grp, list):
                                items = [str(x) for x in grp if x]
                                if not items:
                                    continue
                                if len(items) == 1:
                                    if items[0] not in flat_pr:
                                        flat_pr.append(items[0])
                                    continue
                                prerequisites_groups.append({'type': 'OR', 'items': items})

                    # Mutually exclusive
                    mutex = fx.get('mutually_exclusive', [])
                    if isinstance(mutex, str):
                        mutex = [m.strip() for m in mutex.split(',') if m.strip()]

                    # AI will do
                    try:
                        ai_val = fx.get('ai_will_do')
                        if isinstance(ai_val, list) and ai_val:
                            m = re.search(r'\d+', str(ai_val[0]))
                            ai = int(m.group(0)) if m else 1
                        elif isinstance(ai_val, str):
                            ai = int(re.sub(r'[^0-9]', '', ai_val)) if re.search(r'\d', ai_val) else 1
                        elif isinstance(ai_val, (int, float)):
                            ai = int(ai_val)
                        else:
                            ai = 1
                    except:
                        ai = 1

                    # Icon handling
                    icon = fx.get('icon')
                    icon_path = None

                    # Parser returns either string or list of dicts with conditional icons
                    if isinstance(icon, list) and icon:
                        # Pick first non-conditional icon value
                        for entry in icon:
                            if isinstance(entry, dict):
                                val = entry.get('value')
                                if val and not entry.get('trigger'):
                                    icon_path = val
                                    break
                        # Fallback to first icon if all are conditional
                        if not icon_path and isinstance(icon[0], dict):
                            icon_path = icon[0].get('value')
                    elif isinstance(icon, dict):
                        icon_path = icon.get('value')
                    elif isinstance(icon, str):
                        icon_path = icon

                    import_focuses.append({
                        'id': fid,
                        'name': name,
                        'x': x_val,  # Already absolute coordinates
                        'y': y_val,  # Already absolute coordinates
                        'cost': cost,
                        'description': description,
                        'prerequisites': flat_pr,
                        'mutually_exclusive': mutex,
                        'prerequisites_grouped': bool(prerequisites_groups),
                        'prerequisites_groups': prerequisites_groups,
                        'search_filters': fx.get('search_filters', []),
                        'available': fx.get('available'),
                        'visible': fx.get('visible'),
                        'bypass': fx.get('bypass'),
                        'completion_reward': fx.get('completion_reward'),
                        'select_effect': fx.get('select_effect'),
                        'remove_effect': fx.get('remove_effect'),
                        'cancel': fx.get('cancel'),
                        'complete_tooltip': fx.get('complete_tooltip'),
                        'avail_conditions': fx.get('avail_conditions', []),
                        'ai_will_do': ai,
                        'ai_will_do_block': fx.get('ai_will_do_block'),
                        'allow_branch': fx.get('allow_branch'),
                        'relative_position_id': fx.get('relative_position_id'),
                        'available_if_capitulated': fx.get('available_if_capitulated'),
                        'cancel_if_invalid': fx.get('cancel_if_invalid'),
                        'continue_if_invalid': fx.get('continue_if_invalid'),
                        'will_lead_to_war_with': fx.get('will_lead_to_war_with', []),
                        'network_id': None,
                        'icon': icon_path,
                        'hidden': fx.get('hidden', False),
                        'hidden_tags': fx.get('hidden_tags', []),
                        'raw_unparsed': fx.get('raw_unparsed', []),
                        'has_unparsed': bool(fx.get('has_unparsed')),
                        'clean_raw': fx.get('clean_raw')
                    })

                # Handle icon path resolution - scan for .dds files and resolve GFX_ identifiers
                try:
                    def _collect_gfx_dirs(start_path: str, max_up: int = 6) -> List[str]:
                        seen = []
                        p = start_path or ''
                        for _ in range(max_up):
                            if not p:
                                break
                            cand = os.path.join(p, 'gfx', 'interface', 'goals')
                            if os.path.isdir(cand) and cand not in seen:
                                seen.append(cand)
                            if os.path.isdir(p) and p not in seen:
                                seen.append(p)
                            p = os.path.dirname(p)
                        return seen

                    def _parse_goals_gfx_for_mappings(gfx_path: str) -> Dict[str, str]:
                        mapping: Dict[str, str] = {}
                        try:
                            gf = os.path.join(gfx_path, 'goals.gfx')
                            if os.path.isfile(gf):
                                with open(gf, 'r', encoding='utf-8') as fh:
                                    txt = fh.read()
                                # Match name = "GFX_x" and texturefile = "path"
                                for m in re.finditer(r'name\s*=\s*"?GFX_?([A-Za-z0-9_]+)"?.{0,1024}?texturefile\s*=\s*"([^"]+)"',
                                                    txt, re.IGNORECASE | re.DOTALL):
                                    ident = m.group(1)
                                    tex = m.group(2)
                                    fname = os.path.basename(tex)
                                    if fname:
                                        mapping[ident] = fname
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        return mapping

                    txt_dir = os.path.dirname(filename) if filename else ''
                    gfx_dirs = _collect_gfx_dirs(txt_dir)

                    # Build file map: ident -> fullpath
                    file_map: Dict[str, str] = {}

                    # Parse goals.gfx files
                    for gd in gfx_dirs:
                        gm = _parse_goals_gfx_for_mappings(gd)
                        for ident, fname in gm.items():
                            full = os.path.join(gd, fname)
                            if os.path.isfile(full):
                                file_map[ident] = full

                    # Scan for image files
                    for gd in gfx_dirs:
                        try:
                            for fn in os.listdir(gd):
                                if fn.lower().endswith(('.dds', '.tga', '.png')):
                                    ident = os.path.splitext(fn)[0]
                                    full = os.path.join(gd, fn)
                                    if ident not in file_map:
                                        file_map[ident] = full
                        except:
                            continue

                    # Resolve icon paths
                    for f in import_focuses:
                        icon_raw = f.get('icon')
                        if icon_raw:
                            # Normalize identifier: strip GFX_ prefix and path components
                            ident = icon_raw
                            if isinstance(ident, str):
                                # Remove path if present
                                if any(sep in ident for sep in (os.path.sep, '/', '\\')):
                                    ident = os.path.splitext(os.path.basename(ident))[0]
                                # Remove GFX_ prefix
                                if ident.upper().startswith('GFX_'):
                                    ident = ident[4:]

                                # Look up in file map
                                if ident in file_map:
                                    f['icon'] = file_map[ident]
                                # Keep original if not found
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))  # Non-fatal

                # Build final project dict with resolved focuses
                for f in import_focuses:
                    entry = {
                        'id': f['id'],
                        'name': f.get('name', ''),
                        'x': int(f.get('x', 0)),
                        'y': int(f.get('y', 0)),
                        'cost': int(f.get('cost', 10)),
                        'description': f.get('description', ''),
                        'prerequisites': f.get('prerequisites', []),
                        'mutually_exclusive': f.get('mutually_exclusive', []),
                        'ai_will_do': f.get('ai_will_do', 1)
                    }
                    if f.get('prerequisites_grouped'):
                        entry['prerequisites_grouped'] = True
                    if f.get('prerequisites_groups'):
                        entry['prerequisites_groups'] = f.get('prerequisites_groups', [])
                    if f.get('search_filters'):
                        entry['search_filters'] = f.get('search_filters', [])
                    if f.get('available'):
                        entry['available'] = f.get('available')
                    if f.get('visible'):
                        entry['visible'] = f.get('visible')
                    if f.get('bypass'):
                        entry['bypass'] = f.get('bypass')
                    if f.get('completion_reward'):
                        entry['completion_reward'] = f.get('completion_reward')
                    if f.get('select_effect'):
                        entry['select_effect'] = f.get('select_effect')
                    if f.get('remove_effect'):
                        entry['remove_effect'] = f.get('remove_effect')
                    if f.get('cancel'):
                        entry['cancel'] = f.get('cancel')
                    if f.get('complete_tooltip'):
                        entry['complete_tooltip'] = f.get('complete_tooltip')
                    if f.get('avail_conditions'):
                        entry['avail_conditions'] = f.get('avail_conditions', [])
                    if f.get('ai_will_do_block'):
                        entry['ai_will_do_block'] = f.get('ai_will_do_block')
                    if f.get('allow_branch'):
                        entry['allow_branch'] = f.get('allow_branch')
                    if f.get('relative_position_id'):
                        entry['relative_position_id'] = f.get('relative_position_id')
                    if f.get('available_if_capitulated'):
                        entry['available_if_capitulated'] = True
                    if f.get('cancel_if_invalid'):
                        entry['cancel_if_invalid'] = True
                    if f.get('continue_if_invalid'):
                        entry['continue_if_invalid'] = True
                    if f.get('will_lead_to_war_with'):
                        entry['will_lead_to_war_with'] = f.get('will_lead_to_war_with', [])
                    if f.get('network_id') is not None:
                        entry['network_id'] = f.get('network_id')
                    if f.get('icon'):
                        entry['icon'] = f.get('icon')
                    if f.get('hidden'):
                        entry['hidden'] = True
                    if f.get('hidden_tags'):
                        entry['hidden_tags'] = f.get('hidden_tags', [])
                    if f.get('raw_unparsed'):
                        entry['raw_unparsed'] = f.get('raw_unparsed', [])
                    if f.get('has_unparsed'):
                        entry['has_unparsed'] = True
                    if f.get('clean_raw'):
                        entry['clean_raw'] = f.get('clean_raw')
                    project['focuses'].append(entry)

                # Extract country tag from tree_info
                try:
                    countries = parsed.get('tree_info', {}).get('country', [])
                    if countries and isinstance(countries, (list, tuple)):
                        project['country_tag'] = countries[0]
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Load into application
                try:
                    self.load_project_from_dict(project)
                    # Extra enforcement: some flows may require an explicit re-application
                    # of hidden-branch visibility and a cull to guarantee UI reflects
                    # the imported focus.hidden flags immediately.
                    try:
                        if hasattr(self.canvas, 'apply_hidden_visibility'):
                            try:
                                self.canvas.apply_hidden_visibility()
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # schedule a quick cull/update to ensure hidden nodes are actually hidden
                        try:
                            if hasattr(self.canvas, 'schedule_cull'):
                                self.canvas.schedule_cull()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        try:
                            if hasattr(self.canvas, 'refresh_hidden_branches_menu'):
                                self.canvas.refresh_hidden_branches_menu()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # Inform user and record that we created/used a temp working copy
                    try:
                        if temp_path and os.path.abspath(temp_path) != os.path.abspath(filename):
                            QMessageBox.information(self, "Imported",
                                                    (f"Imported {len(project['focuses'])} focuses from {obfuscate_path(filename)}. "
                                                     f"A temporary working copy was created at: {temp_path}"))
                        else:
                            QMessageBox.information(self, "Imported",
                                                    f"Imported {len(project['focuses'])} focuses from {obfuscate_path(filename)}")
                    except Exception:
                        try:
                            QMessageBox.information(self, "Imported", f"Imported {len(project['focuses'])} focuses")
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.statusBar().showMessage(f"Imported {len(project['focuses'])} focuses")
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # Do not treat imported .txt files or temporary copies as a persisted project path.
                    # Force user to Save As (.json) to establish a proper project file.
                    try:
                        self.current_project_path = None
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    show_error(self, "Load Error", "Failed to load imported project into GUI.", exc=e)

            except Exception as e:
                show_error(self, "Import Error", "Unexpected error during import.", exc=e)
        import_txt_action.triggered.connect(_import_txt)
        file_menu.addAction(import_txt_action)
        file_menu.addSeparator()
        file_menu.addAction(export_panel_action)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        file_menu.addAction(projects_home_action)

        tools_menu = QMenu("Tools", self)
        tools_menu.addAction(gen_action)
        # Insert Prereq Link Mode submenu if available (condensed UI)
        try:
            if hasattr(self, 'prereq_submenu') and self.prereq_submenu is not None:
                tools_menu.addMenu(self.prereq_submenu)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Deprecated Connection Mode: keep available via keybinds only
        # (no menu entry) to avoid confusion with Link Selection.
        tools_menu.addAction(net_color_action)
    # Canvas-only linking UX: do not expose a separate linking menu entry.
    # Linking is performed via canvas clicks (or drag-to-link when enabled in Settings).
        # Notes as a submenu under Tools
        try:
            notes_submenu = QMenu("Notes", tools_menu)
            notes_submenu.addAction(notes_toggle_action)
            notes_submenu.addAction(add_note_action)
            notes_submenu.addAction(find_notes_action)
            notes_submenu.addSeparator()
            notes_submenu.addAction(note_settings_action)
            notes_submenu.addAction(del_notes_action)
            notes_submenu.addSeparator()
            notes_submenu.addAction(clear_notes_action)
            tools_menu.addMenu(notes_submenu)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Append/grow without clearing
        append_action = QAction("Append Generated Nodes…", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        append_action.triggered.connect(self.show_append_dialog)
        tools_menu.addAction(append_action)
        # Provide Icon Library access in Tools as well
        try:
            tools_menu.addAction(icon_lib_action)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Arrangement helpers
        arrange_sel_action = QAction("Arrange Selected Vertically", self)
        # Shortcut handled by KeybindsManager to avoid ambiguity
        arrange_sel_action.triggered.connect(self.arrange_selected_vertically)
        tools_menu.addAction(arrange_sel_action)
        arrange_all_roots_action = QAction("Arrange All by Root Vertically", self)
        arrange_all_roots_action.triggered.connect(self.arrange_all_by_root_vertically)
        tools_menu.addAction(arrange_all_roots_action)
        # Batch delete (no default shortcut; Delete handled centrally by the view)
        del_sel_action = QAction("Delete Selected Focuses", self)
        del_sel_action.triggered.connect(self.delete_selected_focuses)
        tools_menu.addAction(del_sel_action)
        # Game State Dock toggle
        try:
            self.game_state_dock = GameStateDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.game_state_dock)
            # keep hidden by default
            self.game_state_dock.hide()
            gs_action = QAction("Game State...", self)
            gs_action.setCheckable(True)
            def _toggle_gs(chk):
                try:
                    if chk:
                        self.game_state_dock.show()
                        # Repopulate list from current focuses when shown so the
                        # user sees an up-to-date set immediately.
                        try:
                            self.game_state_dock.populate(getattr(self, 'focuses', []) or [])
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    else:
                        self.game_state_dock.hide()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            gs_action.toggled.connect(_toggle_gs)
            tools_menu.addAction(gs_action)
            self._game_state_action = gs_action
            # Ensure the dock's state_changed drives a visual refresh of nodes
            try:
                def _on_game_state_change():
                    try:
                        for n in list(getattr(self, 'canvas', None).nodes.values()):
                            try:
                                n.update()
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.game_state_dock.state_changed.connect(_on_game_state_change)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Compact connected layout
        compact_action = QAction("Compact Connected Layout", self)
        def _do_compact():
            try:
                if getattr(self.canvas, 'auto_layout_enabled', False):
                    self.canvas.compact_connected_layout()
                else:
                    try:
                        self.statusBar().showMessage('Auto-layout disabled', 1500)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        compact_action.triggered.connect(_do_compact)
        tools_menu.addAction(compact_action)

        view_menu = QMenu("View", self)
        # Zoom controls removed from View menu; wheel zoom is the supported mechanism
        # (zoom_in_action, zoom_out_action, fit_action are not added to menu)
        view_menu.addSeparator()
        view_menu.addAction(frames_action)
        view_menu.addAction(lineage_color_action)
        view_menu.addAction(grid_action)
        # Icon View toggle
        icon_view_action = QAction("Icon View Mode", self)
        icon_view_action.setCheckable(True)
        icon_view_action.setChecked(False)
        def _toggle_icon_view(chk):
            try:
                self.canvas.icon_view_mode = bool(chk)
                # refresh nodes
                for n in list(self.canvas.nodes.values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # persist setting
                self.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        icon_view_action.toggled.connect(_toggle_icon_view)
        view_menu.addAction(icon_view_action)
        self.icon_view_action = icon_view_action
        # Auto-Layout toggle
        auto_layout_action = QAction("Auto-Layout (reflow/compact)", self)
        auto_layout_action.setCheckable(True)
        # If canvas isn't created yet, use a pending flag; otherwise reflect the canvas setting
        try:
            initial = False
            if hasattr(self, 'canvas') and getattr(self.canvas, 'auto_layout_enabled', None) is not None:
                initial = bool(getattr(self.canvas, 'auto_layout_enabled', False))
            else:
                initial = bool(getattr(self, '_pending_auto_layout', False))
            auto_layout_action.setChecked(initial)
        except Exception:
            try:
                auto_layout_action.setChecked(False)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        def _toggle_auto_layout(chk):
            try:
                # Record pending flag so the state persists even if canvas isn't created yet
                self._pending_auto_layout = bool(chk)
                if hasattr(self, 'canvas') and self.canvas is not None:
                    try:
                        self.canvas.auto_layout_enabled = bool(chk)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        auto_layout_action.toggled.connect(_toggle_auto_layout)
        view_menu.addAction(auto_layout_action)
        # Hidden Branches submenu: dynamic per-tag toggles
        try:
            hidden_sub = QMenu("Hidden Branches", self)

            # master toggle: show all hidden branches
            show_all_action = QAction("Show Hidden Branches", self)
            show_all_action.setCheckable(True)
            # initial state: any tag visible -> True
            def _compute_master_initial():
                try:
                    tags = list(getattr(self.canvas, '_hidden_tag_index', {}).keys())
                    for t in tags:
                        if getattr(self.canvas, '_show_hidden_branches_by_tag', {}).get(t, False):
                            return True
                    return False
                except Exception:
                    return False

            show_all_action.setChecked(_compute_master_initial())

            def _toggle_show_all(chk):
                try:
                    # set all known tags to chk and apply visibility
                    tags = list(getattr(self.canvas, '_hidden_tag_index', {}).keys())
                    for t in tags:
                        try:
                            self.canvas._show_hidden_branches_by_tag[t] = bool(chk)
                            for node in list(self.canvas._hidden_tag_index.get(t, []) or []):
                                try:
                                    # only change nodes that were hidden due to tag membership
                                    if getattr(node, 'focus', None) and getattr(node.focus, 'hidden', False):
                                        try:
                                            # mark this as a user-driven override so automated culling won't undo it
                                            node.set_logical_visible(bool(chk), user=True)
                                        except Exception:
                                            node.setVisible(bool(chk))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # update per-tag QAction checked state if present
                    try:
                        for act in list(hidden_sub.actions())[2:]:
                            try:
                                if hasattr(act, 'setChecked') and act.isCheckable():
                                    act.setChecked(bool(chk))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # persist preference
                    try:
                        self.save_settings()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            show_all_action.toggled.connect(_toggle_show_all)
            hidden_sub.addAction(show_all_action)
            hidden_sub.addSeparator()

            # populate per-tag actions lazily; provide a refresh function to rebuild submenu
            def _refresh_hidden_submenu():
                try:
                    # remove existing tag actions (preserve first two entries: master + separator)
                    for act in list(hidden_sub.actions())[2:]:
                        try:
                            hidden_sub.removeAction(act)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    tags = sorted(list(getattr(self.canvas, '_hidden_tag_index', {}).keys()))
                    if not tags:
                        noact = QAction("(no hidden branches detected)", self)
                        noact.setEnabled(False)
                        hidden_sub.addAction(noact)
                        return
                    for t in tags:
                        try:
                            a = QAction(str(t), self)
                            a.setCheckable(True)
                            a.setChecked(bool(self.canvas._show_hidden_branches_by_tag.get(t, False)))
                            def make_toggle(tag):
                                def _toggle(chk):
                                    try:
                                        self.canvas._show_hidden_branches_by_tag[tag] = bool(chk)
                                        for node in list(self.canvas._hidden_tag_index.get(tag, []) or []):
                                            try:
                                                if getattr(node, 'focus', None) and getattr(node.focus, 'hidden', False):
                                                    try:
                                                        node.set_logical_visible(bool(chk), user=True)
                                                    except Exception:
                                                        node.setVisible(bool(chk))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        try:
                                            self.save_settings()
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                return _toggle
                            a.toggled.connect(make_toggle(t))
                            hidden_sub.addAction(a)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # attach refresh helper to the canvas so other code (load_project) can call it
            try:
                setattr(self.canvas, 'refresh_hidden_branches_menu', _refresh_hidden_submenu)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # trigger initial population
            _refresh_hidden_submenu()
            view_menu.addMenu(hidden_sub)
            self._hidden_branches_menu = hidden_sub
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Visualizer: Connection Lineage Mode
        lineage_vis_action = QAction("Visualizer: Connection Lineage Mode", self)
        lineage_vis_action.setCheckable(True)
        lineage_vis_action.setChecked(False)
        def _toggle_lineage_vis(chk):
            try:
                enabled = bool(chk)
                try:
                    if hasattr(self.canvas, 'set_visualizer_lineage_mode'):
                        self.canvas.set_visualizer_lineage_mode(enabled)
                        return
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # fallback to the old behavior if atomic setter isn't available
                if enabled:
                    # Hide frames and recolor connections per leaf lineage
                    self.canvas._prev_frames_enabled = self.canvas.frames_enabled
                    self.canvas.frames_enabled = False
                    self.canvas.clear_frames()
                    self.canvas._leaf_dirty = True
                    # Pack connected focuses closer for clearer lineage visualization
                    self.canvas.compact_connected_layout()
                else:
                    # Restore frames
                    self.canvas.frames_enabled = self.canvas._prev_frames_enabled
                    self.canvas._frames_dirty = True
                    self.canvas.schedule_frame_update()
                self.canvas.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        lineage_vis_action.toggled.connect(_toggle_lineage_vis)
        view_menu.addAction(lineage_vis_action)
        # Layer manager action
        layer_manager_action = QAction("Layer Manager...", self)
        def _open_layer_manager():
            try:
                dlg = LayerManagerDialog(self.canvas, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    # apply visibility settings
                    for lid, vis in dlg.get_visibilities().items():
                        self.canvas.layer_visibility[lid] = vis
                    # mark dirty and refresh
                    self.canvas._frames_dirty = True
                    self.canvas.schedule_frame_update()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        layer_manager_action.triggered.connect(_open_layer_manager)
        view_menu.addAction(layer_manager_action)

        # Add a discoverable View menu entry to toggle the State Viewport dock
        try:
            show_state_viewport_action = QAction("Show State Viewport", self)
            show_state_viewport_action.setCheckable(True)
            # initial checked state reflects current dock visibility (if present)
            try:
                visible = bool(getattr(self, 'state_viewport_dock', None) and getattr(self.state_viewport_dock, 'isVisible', lambda: False)())
                show_state_viewport_action.setChecked(visible)
            except Exception:
                try:
                    show_state_viewport_action.setChecked(False)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            def _toggle_state_viewport_from_menu(checked: bool):
                try:
                    if getattr(self, 'state_viewport_dock', None) is not None:
                        self.state_viewport_dock.setVisible(bool(checked))
                    # keep toolbar toggle in sync if present
                    if getattr(self, 'toggle_state_viewport_action', None) is not None:
                        try:
                            self.toggle_state_viewport_action.setChecked(bool(checked))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            show_state_viewport_action.toggled.connect(_toggle_state_viewport_from_menu)
            view_menu.addAction(show_state_viewport_action)
            self.show_state_viewport_action = show_state_viewport_action
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Edit menu (clipboard and batch ops)
        edit_menu = QMenu("Edit", self)
        # Undo / Redo
        try:
            undo_action = QAction("Undo", self)
            redo_action = QAction("Redo", self)
            # Standard shortcuts
            try:
                undo_action.setShortcut(QKeySequence.StandardKey.Undo)
                redo_action.setShortcut(QKeySequence.StandardKey.Redo)
            except Exception:
                # Fallback to explicit shortcuts (Ctrl+Z / Ctrl+Y)
                undo_action.setShortcut("Ctrl+Z")
                redo_action.setShortcut("Ctrl+Y")
            if getattr(self, 'undo_stack', None) is not None:
                # Wire the actions to the stack and enable/disable them based on
                # whether there are undo/redo commands available. Use the
                # QUndoStack signals so the UI updates automatically.
                try:
                    undo_action.triggered.connect(lambda: self.undo_stack.undo())
                    redo_action.triggered.connect(lambda: self.undo_stack.redo())
                    # Update enabled state when the stack's ability changes
                    self.undo_stack.canUndoChanged.connect(undo_action.setEnabled)
                    self.undo_stack.canRedoChanged.connect(redo_action.setEnabled)
                    # Initialize enabled state
                    undo_action.setEnabled(self.undo_stack.canUndo())
                    redo_action.setEnabled(self.undo_stack.canRedo())
                except Exception:
                    # Fallback to always enabled if signals or methods aren't
                    # present for some reason, but ensure actions exist.
                    try:
                        undo_action.triggered.connect(lambda: self.undo_stack.undo())
                        redo_action.triggered.connect(lambda: self.undo_stack.redo())
                    except Exception:
                        undo_action.setEnabled(False); redo_action.setEnabled(False)
            else:
                # No undo stack available — disable the actions
                undo_action.setEnabled(False); redo_action.setEnabled(False)
            edit_menu.addAction(undo_action)
            edit_menu.addAction(redo_action)
            edit_menu.addSeparator()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        edit_menu.addAction(copy_action)
        edit_menu.addAction(paste_action)
        edit_menu.addSeparator()
        edit_menu.addAction(multi_add_action)
        edit_menu.addAction(colorize_action)
        edit_menu.addSeparator()
        edit_menu.addAction(del_sel_action)

        # Notes top-level menu removed; now provided as a submenu under Tools

        # Settings action
        settings_action = QAction("Settings…", self)
        def _open_settings():
            try:
                dlg = SettingsDialog(self.canvas, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    try:
                        self.save_settings()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                show_error(self, "Settings Error", "Failed to open settings dialog.", exc=e)
                import traceback
                traceback.print_exc()
        settings_action.triggered.connect(_open_settings)

        lib_menu = QMenu("Library", self)
        lib_menu.addAction(save_lib_action)
        lib_menu.addAction(load_lib_action)
        # Also provide access to Icon Library here for convenience
        lib_menu.addSeparator()
        lib_menu.addAction(icon_lib_action)

        help_menu = QMenu("Help", self)
        check_update_action = QAction("Check for Updates...", self)
        check_update_action.triggered.connect(self.check_for_updates)
        # The built-in updater currently downloads Windows .exe assets only.
        # Keep the action visible on Linux but disable it with a clear message.
        if not sys.platform.startswith('win'):
            check_update_action.setEnabled(False)
            check_update_action.setToolTip("Self-update is currently Windows-only. Use your package manager/AppImage release on Linux.")
        help_menu.addAction(check_update_action)
        try:
            self.menuBar().addMenu(help_menu)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Add menu actions to the toolbar (condensed UI)
        toolbar.addAction(file_menu.menuAction())
        toolbar.addAction(edit_menu.menuAction())
        toolbar.addAction(tools_menu.menuAction())
        toolbar.addAction(view_menu.menuAction())
        toolbar.addAction(lib_menu.menuAction())
        # Dedicated Settings button (toolbar) — opens full settings panel
        settings_btn = QAction("Settings", self)
        settings_btn.setToolTip("Open application settings")
        def _open_settings_btn():
            try:
                dlg = SettingsDialog(self.canvas, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    try:
                        self.save_settings()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                show_error(self, "Settings", "Failed to open settings.", exc=e)
        settings_btn.triggered.connect(_open_settings_btn)
        toolbar.addAction(settings_btn)
        # Tree settings
        toolbar.addWidget(QLabel("Tree ID:"))
        self.tree_id_edit = QLineEdit(self.tree_id)
        self.tree_id_edit.setMaximumWidth(150)
        self.tree_id_edit.textChanged.connect(lambda t: setattr(self, 'tree_id', t))
        toolbar.addWidget(self.tree_id_edit)

        toolbar.addWidget(QLabel("Country:"))
        self.country_edit = QLineEdit(self.country_tag)
        self.country_edit.setMaximumWidth(60)
        self.country_edit.textChanged.connect(lambda t: setattr(self, 'country_tag', t))
        toolbar.addWidget(self.country_edit)

        # Library dock (dockable panel)
        self.library_dock = QDockWidget("Focus Library", self)
        self.library_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        # Make the panel slightly smaller and collapsible
        try:
            self.library_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
            self.library_dock.setMinimumWidth(260)
            # Allow user to expand the library panel; remove restrictive max width
            # Keep a sensible minimum width so content remains usable
            # self.library_dock.setMaximumWidth(420)
            self.library_dock.setFloating(False)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.library_dock)
        self.library_widget = QWidget()
        lib_layout = QVBoxLayout()
        # Search/filter and small library controls (sort/group)
        search_layout = QHBoxLayout()
        self.lib_search = QLineEdit()
        self.lib_search.setPlaceholderText("Filter library (id or name)...")
        self.lib_search.textChanged.connect(self.refresh_library_list)
        search_layout.addWidget(self.lib_search)

        # Add State Viewport dock (if available)
        try:
            if StateViewportDock is not None:
                self.state_viewport_dock = StateViewportDock(parent=self)
                # start hidden/collapsed by default
                try:
                    self.state_viewport_dock.setVisible(False)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.state_viewport_dock)
                # connect selection signal; avoid noisy console logging
                try:
                    self.state_viewport_dock.state_selection_changed.connect(lambda s: logger.debug('State selection changed'))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # Create a toolbar toggle action for discoverability and quick toggling
                try:
                    toggle = QAction('State Viewport', self)
                    toggle.setCheckable(True)
                    toggle.setChecked(False)
                    def _toggle_dock(checked: bool):
                        try:
                            if getattr(self, 'state_viewport_dock', None) is not None:
                                self.state_viewport_dock.setVisible(bool(checked))
                            # keep menu action in sync
                            try:
                                if getattr(self, 'show_state_viewport_action', None) is not None:
                                    self.show_state_viewport_action.setChecked(bool(checked))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    toggle.toggled.connect(_toggle_dock)
                    try:
                        toolbar.addAction(toggle)
                        self.toggle_state_viewport_action = toggle
                    except Exception:
                        # fallback: attach to main toolbar reference if available
                        try:
                            if getattr(self, '_main_toolbar', None) is not None:
                                self._main_toolbar.addAction(toggle)
                                self.toggle_state_viewport_action = toggle
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # keep toggle in sync if the dock is closed via its close button
                    try:
                        self.state_viewport_dock.visibilityChanged.connect(lambda v: setattr(self.toggle_state_viewport_action, 'checked', bool(v)) if getattr(self, 'toggle_state_viewport_action', None) is not None else None)
                        self.state_viewport_dock.visibilityChanged.connect(lambda v: setattr(self.show_state_viewport_action, 'checked', bool(v)) if getattr(self, 'show_state_viewport_action', None) is not None else None)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception:
            self.state_viewport_dock = None
        # Sort combo
        self.lib_sort_combo = QComboBox()
        self.lib_sort_combo.addItems(["Name", "Key", "Category"])
        self.lib_sort_combo.setCurrentIndex(0)
        self.lib_sort_combo.setToolTip("Sort library entries")
        self.lib_sort_combo.currentIndexChanged.connect(self.refresh_library_list)
        search_layout.addWidget(QLabel("Sort:"))
        search_layout.addWidget(self.lib_sort_combo)
        # Collapse categories checkbox
        self.lib_collapse_checkbox = QCheckBox("Collapse categories")
        self.lib_collapse_checkbox.setChecked(True)
        self.lib_collapse_checkbox.stateChanged.connect(self.refresh_library_list)
        search_layout.addWidget(self.lib_collapse_checkbox)
        lib_layout.addLayout(search_layout)
        # Library area: overview cards + per-folder tree views (stacked)
        # Overview widget will show folder cards; per-folder views are QTreeWidget instances inside a QStackedWidget
        self.lib_stack = QStackedWidget()

        # Overview page with folder cards inside a scroll area
        overview = QWidget()
        overview_layout = QVBoxLayout()
        overview.setLayout(overview_layout)
        self.lib_overview_scroll = QScrollArea()
        self.lib_overview_scroll.setWidgetResizable(True)
        self.lib_overview_container = QWidget()
        self.lib_overview_layout = QGridLayout()
        self.lib_overview_container.setLayout(self.lib_overview_layout)
        self.lib_overview_scroll.setWidget(self.lib_overview_container)
        overview_layout.addWidget(self.lib_overview_scroll)

        self.lib_stack.addWidget(overview)

        # Mapping folder_id -> QTreeWidget (populated by refresh_library_list)
        self._folder_trees = {}

        lib_layout.addWidget(self.lib_stack)
        # Buttons
        lib_btn_layout = QHBoxLayout()
        apply_btn = QPushButton("Apply to Selected Focus")
        apply_btn.clicked.connect(self.apply_library_to_selected_focus)
        create_btn = QPushButton("Create Focus from Entry")
        create_btn.clicked.connect(self.create_focus_from_library_selected)
        save_sel_btn = QPushButton("Save Selected Focus to Library")
        save_sel_btn.clicked.connect(self.save_selected_focus_to_library)
        del_btn = QPushButton("Delete Entry")
        del_btn.clicked.connect(self.delete_selected_library_entry)
        lib_btn_layout.addWidget(apply_btn)
        lib_btn_layout.addWidget(create_btn)
        lib_btn_layout.addWidget(save_sel_btn)
        lib_btn_layout.addWidget(del_btn)
        lib_layout.addLayout(lib_btn_layout)
        # Bottom quick actions
        quick_layout = QHBoxLayout()
        import_btn = QPushButton("Import Library (JSON)")
        import_btn.clicked.connect(self.load_library_from_file)
        export_btn = QPushButton("Export Library (JSON)")
        export_btn.clicked.connect(self.save_library_to_file)
        quick_layout.addWidget(import_btn)
        quick_layout.addWidget(export_btn)
        clear_db_btn = QPushButton("Clear Database")
        clear_db_btn.setToolTip("Clear the on-disk library database and in-memory entries")
        clear_db_btn.clicked.connect(self.clear_library_database)
        quick_layout.addWidget(clear_db_btn)
        lib_layout.addLayout(quick_layout)
        # Add a resize grip so docked widget can be resized by dragging
        grip_row = QHBoxLayout()
        grip_row.addStretch()
        size_grip = QSizeGrip(self.library_widget)
        grip_row.addWidget(size_grip)
        lib_layout.addLayout(grip_row)

        self.library_widget.setLayout(lib_layout)
        self.library_dock.setWidget(self.library_widget)

        # Toolbar toggle to show/hide library panel (DISABLED BY DEFAULT)
        lib_toggle_action = QAction("Show Library", self)
        lib_toggle_action.setCheckable(True)
        lib_toggle_action.setChecked(False)  # Disabled by default
        def _toggle_lib(chk):
            try:
                self.library_dock.setVisible(bool(chk))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        lib_toggle_action.toggled.connect(_toggle_lib)
        # Hide the library dock by default
        self.library_dock.setVisible(False)
        toolbar.addAction(lib_toggle_action)
        self.lib_toggle_action = lib_toggle_action

        # Enhanced graphics view (canvas/view already created earlier; reuse)
        if getattr(self, 'canvas', None) is None:
            self.canvas = FocusTreeCanvas(self)
        if getattr(self, 'view', None) is None:
            self.view = EnhancedGraphicsView(self.canvas)
        layout.addWidget(self.view)
        # Apply any pending auto-layout preference recorded earlier (or from settings)
        try:
            if getattr(self, '_pending_auto_layout', None) is not None:
                try:
                    self.canvas.auto_layout_enabled = bool(self._pending_auto_layout)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                # ensure canvas reflects any persisted setting loaded into self.canvas already
                try:
                    self.canvas.auto_layout_enabled = bool(getattr(self.canvas, 'auto_layout_enabled', False))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Ctrl+N handled by KeybindsManager; no extra QShortcut here to avoid conflicts
        # After view/canvas exist, try to set up defaults for HOI4 pill image path
        try:
            self._setup_default_pill_image()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # The Settings UI is built earlier than the canvas, so ensure controls
        # that depend on canvas defaults are synced now that the canvas exists.
        try:
            try:
                # Event fonts
                if hasattr(self, 'event_opt_font_spin'):
                    try:
                        self.event_opt_font_spin.setValue(int(getattr(self.canvas, 'event_options_font_size', 10)))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                if hasattr(self, 'event_desc_font_spin'):
                    try:
                        self.event_desc_font_spin.setValue(int(getattr(self.canvas, 'event_desc_font_size', 10)))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                if hasattr(self, 'event_title_font_spin'):
                    try:
                        self.event_title_font_spin.setValue(int(getattr(self.canvas, 'event_title_font_size', 14)))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Render node ids toggle
                if hasattr(self, 'render_node_ids_chk'):
                    try:
                        self.render_node_ids_chk.setChecked(bool(getattr(self.canvas, 'render_node_ids', True)))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Render XY controls (per-item offsets)
                try:
                    for (kx, ky), (sx_ctrl, sy_ctrl) in list(getattr(self, '_render_xy_controls', {}).items()):
                        try:
                            sx_ctrl.setValue(int(getattr(self.canvas, kx, 0)))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        try:
                            sy_ctrl.setValue(int(getattr(self.canvas, ky, 0)))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Status bar
        status_bar = self.statusBar()
        status_bar.showMessage("Ready - Use middle mouse to pan, wheel to zoom, right-click for context menu")
        # Guard against duplicate creation if UI init runs more than once
        if not hasattr(self, 'zoom_label'):
            self.zoom_label = QLabel("100%")
            self.focus_count_label = QLabel("Focuses: 0")
            try:
                status_bar.addPermanentWidget(self.focus_count_label)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                status_bar.addPermanentWidget(self.zoom_label)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        else:
            # If labels exist but aren't parented to the status bar, add them once
            try:
                if hasattr(self, 'focus_count_label') and self.focus_count_label.parent() is not status_bar:
                    status_bar.addPermanentWidget(self.focus_count_label)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if hasattr(self, 'zoom_label') and self.zoom_label.parent() is not status_bar:
                    status_bar.addPermanentWidget(self.zoom_label)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # update status timer
        self.update_status()
        # Always prompt the Projects Home dialog; never auto-load prior projects
        try:
            self.show_projects_home()
        except Exception:
            self.show_projects_home()

    def check_for_updates(self):
        """Check for updates in background thread"""
        # Guard Linux/macOS from entering the Windows-only updater flow.
        if not sys.platform.startswith('win'):
            QMessageBox.information(
                self,
                "Updater Not Available",
                "In-app updates are currently Windows-only.\n"
                "On Linux, please update through your package manager or by downloading a new AppImage release.",
            )
            return

        print("DEBUG: check_for_updates called")

        if GitHubUpdater is None:
            QMessageBox.critical(self, "Error", "Updater module not available.")
            return

        self.statusBar().showMessage("Checking for updates...")

        # Create updater on main thread
        OWNER = GITHUB_REPO_OWNER
        REPO = GITHUB_REPO_NAME
        CURRENT_VERSION = self.app_version  # Use loaded version from version.txt

        updater = GitHubUpdater(OWNER, REPO, CURRENT_VERSION)

        def _check():
            try:
                print("DEBUG: Thread starting check")
                has_update = updater.check_for_updates()
                print(f"DEBUG: has_update = {has_update}")

                # Emit signals which are thread-safe and will be delivered to main thread
                if has_update:
                    print("DEBUG: Emitting update_available_signal")
                    try:
                        self.update_available_signal.emit(updater)
                    except Exception as emit_exc:
                        print(f"DEBUG: emit failed: {emit_exc}")
                else:
                    print("DEBUG: Emitting no_update_signal")
                    try:
                        self.no_update_signal.emit()
                    except Exception as emit_exc:
                        print(f"DEBUG: emit failed: {emit_exc}")

            except Exception as e:
                print(f"DEBUG: Exception: {e}")
                import traceback
                traceback.print_exc()

        thread = threading.Thread(target=_check, daemon=True)
        thread.start()

    # Add these two methods to your class:
    def show_update_dialog(self, updater):
        print("DEBUG: show_update_dialog called on main thread")
        reply = QMessageBox.question(
            self,
            "Update Available",
            f"New version {updater.latest_version} is available!\n"
            f"Current version: {updater.current_version}\n\n"
            "Download and install now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            updater.run_update()

    def show_uptodate_dialog(self):
        print("DEBUG: show_uptodate_dialog called on main thread")
        QMessageBox.information(self, "Up to Date", "You have the latest version.")

    def load_settings(self):
        """Load persisted settings from disk and apply to canvas and UI controls."""
        path = getattr(self, 'settings_path', None)
        if not path:
            return
        with operation_context("load_settings", module=__name__, file_path=path):
            try:
                if not os.path.exists(path):
                    return
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except FileNotFoundError as e:
                handle_exception(
                    FileOperationError("Settings file not found", original_exception=e, path=path),
                    policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="warning", show_traceback=False),
                    parent=self,
                )
                return
            except json.JSONDecodeError as e:
                handle_exception(
                    SerializationError("Settings file is invalid JSON", original_exception=e, path=path),
                    policy=PolicyConfig(policy=ErrorPolicy.GUI_NOTIFY, log_level="error", show_traceback=True, user_message="Settings file is corrupted."),
                    parent=self,
                )
                return
            except Exception as e:
                handle_exception(
                    ConfigurationError("Failed to read settings file", original_exception=e, path=path),
                    policy=PolicyConfig(policy=ErrorPolicy.GUI_NOTIFY, log_level="error", show_traceback=True, user_message="Unable to load settings."),
                    parent=self,
                )
                return
            try:
                # Keybindings first (so UI reflects correct shortcuts)
                try:
                    kb = data.get('keybindings', {}) if isinstance(data, dict) else {}
                    if kb and getattr(self, 'keybinds', None) is not None:
                        self.keybinds.apply_mapping(kb)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                if isinstance(data, dict) and 'canvas' in data:
                    # Apply app-level canvas settings from the main settings file first
                    try:
                        self.canvas.apply_settings(data['canvas'])
                    except Exception as e:
                        handle_exception(
                            ConfigurationError("Failed to apply main canvas settings", original_exception=e, path=path),
                            policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="warning", show_traceback=False),
                            parent=self,
                        )
                    try:
                        # start cache from the main settings file; it may be overlaid by a dedicated
                        # `app_settings.json` when the user has enabled app-wide preferences.
                        self._app_canvas_settings_cache = dict(data['canvas']) if isinstance(data['canvas'], dict) else None
                    except Exception:
                        self._app_canvas_settings_cache = None
                    # Ensure any existing Settings UI controls reflect the newly-applied canvas values
                    try:
                        # Event font controls
                        if hasattr(self, 'event_opt_font_spin'):
                            try:
                                self.event_opt_font_spin.setValue(int(getattr(self.canvas, 'event_options_font_size', 10)))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        if hasattr(self, 'event_desc_font_spin'):
                            try:
                                self.event_desc_font_spin.setValue(int(getattr(self.canvas, 'event_desc_font_size', 10)))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        if hasattr(self, 'event_title_font_spin'):
                            try:
                                self.event_title_font_spin.setValue(int(getattr(self.canvas, 'event_title_font_size', 14)))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # Render node ids toggle
                        if hasattr(self, 'render_node_ids_chk'):
                            try:
                                self.render_node_ids_chk.setChecked(bool(getattr(self.canvas, 'render_node_ids', True)))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # Render XY controls (per-item offsets)
                        try:
                            for (kx, ky), (sx_ctrl, sy_ctrl) in list(getattr(self, '_render_xy_controls', {}).items()):
                                try:
                                    sx_ctrl.setValue(int(getattr(self.canvas, kx, 0)))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                try:
                                    sy_ctrl.setValue(int(getattr(self.canvas, ky, 0)))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # load top-level paths and logging config
                try:
                    paths = data.get('paths', {}) or {}
                    # if an app_base_dir was stored, restore it first
                    abd = paths.get('app_base_dir') or paths.get('app_base') or None
                    if abd:
                        try:
                            self.app_base_dir = str(abd)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # ensure directories exist for the app_base_dir before other defaults
                    try:
                        if getattr(self, 'app_base_dir', None):
                            self.ensure_app_dirs()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    if 'settings_path' in paths:
                        self.settings_path = str(paths.get('settings_path'))
                    if 'database_path' in paths:
                        self.database_path = str(paths.get('database_path'))
                    if 'icon_library_path' in paths:
                        try:
                            self.icon_library_path = str(paths.get('icon_library_path') or '')
                        except Exception:
                            try:
                                self.icon_library_path = ''
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # attempt to scan icons for the restored path (UI may not be ready yet)
                        try:
                            self.scan_icon_library()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    if 'projects_home_path' in paths:
                        p = str(paths.get('projects_home_path') or '')
                        if not p:
                            abd = getattr(self, 'app_base_dir', None)
                            if abd:
                                p = os.path.join(abd, 'projects')
                            else:
                                p = os.getcwd()
                        self.projects_home_path = p
                    if 'current_project_path' in paths:
                        restored_project_path = str(paths.get('current_project_path') or '')
                        if restored_project_path and os.path.isfile(restored_project_path):
                            self.current_project_path = restored_project_path
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # preferences (app-wide behavior)
                try:
                    prefs = data.get('preferences', {}) or {}
                    # set preference early so we can decide whether to load a dedicated
                    # `app_settings.json` file which may override the canvas settings above.
                    try:
                        # Apply basic preference values to attributes and UI controls
                        self.muted = bool(prefs.get('muted', getattr(self, 'muted', False)))
                        self.autosave_enabled = bool(prefs.get('autosave_enabled', getattr(self, 'autosave_enabled', False)))
                        try:
                            self.autosave_interval_min = int(prefs.get('autosave_interval_min', getattr(self, 'autosave_interval_min', 5)))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        self.autosave_overwrite = bool(prefs.get('autosave_overwrite', getattr(self, 'autosave_overwrite', True)))
                        self.autosave_rotate = bool(prefs.get('autosave_rotate', getattr(self, 'autosave_rotate', False)))
                        try:
                            self.autosave_rotate_count = int(prefs.get('autosave_rotate_count', getattr(self, 'autosave_rotate_count', 6)))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # Normalize mutually exclusive flags (rotate wins over overwrite)
                        try:
                            if self.autosave_rotate:
                                self.autosave_overwrite = False
                            elif self.autosave_overwrite:
                                self.autosave_rotate = False
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # reflect into UI controls if present
                        try:
                            if hasattr(self, 'mute_action') and self.mute_action is not None:
                                try:
                                    self.mute_action.setChecked(bool(self.muted))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        try:
                            if hasattr(self, 'autosave_chk') and self.autosave_chk is not None:
                                try:
                                    self.autosave_chk.setChecked(bool(self.autosave_enabled))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            if hasattr(self, 'autosave_interval_spin') and self.autosave_interval_spin is not None:
                                try:
                                    self.autosave_interval_spin.setValue(int(self.autosave_interval_min))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            if hasattr(self, 'autosave_overwrite_chk') and self.autosave_overwrite_chk is not None:
                                try:
                                    self.autosave_overwrite_chk.setChecked(bool(self.autosave_overwrite))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            if hasattr(self, 'autosave_rotate_chk') and self.autosave_rotate_chk is not None:
                                try:
                                    self.autosave_rotate_chk.setChecked(bool(self.autosave_rotate))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            if hasattr(self, 'autosave_rotate_spin') and self.autosave_rotate_spin is not None:
                                try:
                                    self.autosave_rotate_spin.setValue(int(self.autosave_rotate_count))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            # enable/disable rotate count based on rotate toggle
                            try:
                                if hasattr(self, 'autosave_rotate_spin') and self.autosave_rotate_spin is not None:
                                    self.autosave_rotate_spin.setEnabled(bool(self.autosave_rotate))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # ensure canvas repaints after settings applied
                        self.canvas.schedule_frame_update()
                    except Exception:
                        try:
                            self.canvas.update()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception:
                    self.prefer_app_settings = False

                # If the user prefers app-wide settings, attempt to load an auxiliary
                # `app_settings.json` file located next to the main settings file and
                # apply its canvas settings (this allows exporting a portable, app-wide
                # settings blob that can be shared between machines/projects).
                try:
                    if bool(getattr(self, 'prefer_app_settings', False)):
                        # Determine candidate path next to the main settings file
                        try:
                            base_dir = os.path.dirname(path) or os.getcwd()
                        except Exception:
                            base_dir = os.getcwd()
                        app_settings_file = os.path.join(base_dir, 'app_settings.json')
                        app_loaded = False
                        # 1) Try dedicated app_settings.json
                        if os.path.exists(app_settings_file) and os.path.isfile(app_settings_file):
                            try:
                                with open(app_settings_file, 'r', encoding='utf-8') as af:
                                    app_data = json.load(af)
                                if isinstance(app_data, dict) and 'canvas' in app_data:
                                    # apply canvas
                                    try:
                                        self.canvas.apply_settings(app_data['canvas'])
                                    except Exception as e:
                                        handle_exception(
                                            ConfigurationError("Failed to apply app-wide canvas settings", original_exception=e, path=app_settings_file),
                                            policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="warning", show_traceback=False),
                                            parent=self,
                                        )
                                    try:
                                        self._app_canvas_settings_cache = dict(app_data['canvas']) if isinstance(app_data['canvas'], dict) else self._app_canvas_settings_cache
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    # apply preferences from app settings
                                    try:
                                        aprefs = app_data.get('preferences', {}) or {}
                                        self.muted = bool(aprefs.get('muted', getattr(self, 'muted', False)))
                                        self.autosave_enabled = bool(aprefs.get('autosave_enabled', getattr(self, 'autosave_enabled', False)))
                                        try:
                                            self.autosave_interval_min = int(aprefs.get('autosave_interval_min', getattr(self, 'autosave_interval_min', 5)))
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        self.autosave_overwrite = bool(aprefs.get('autosave_overwrite', getattr(self, 'autosave_overwrite', True)))
                                        self.autosave_rotate = bool(aprefs.get('autosave_rotate', getattr(self, 'autosave_rotate', False)))
                                        try:
                                            self.autosave_rotate_count = int(aprefs.get('autosave_rotate_count', getattr(self, 'autosave_rotate_count', 6)))
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        # Normalize exclusivity
                                        try:
                                            if self.autosave_rotate:
                                                self.autosave_overwrite = False
                                            elif self.autosave_overwrite:
                                                self.autosave_rotate = False
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    # reflect to UI controls if they exist
                                    try:
                                        if hasattr(self, 'mute_action') and self.mute_action is not None:
                                            try:
                                                self.mute_action.setChecked(bool(self.muted))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    try:
                                        if hasattr(self, 'autosave_chk') and self.autosave_chk is not None:
                                            try:
                                                self.autosave_chk.setChecked(bool(self.autosave_enabled))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        if hasattr(self, 'autosave_interval_spin') and self.autosave_interval_spin is not None:
                                            try:
                                                self.autosave_interval_spin.setValue(int(self.autosave_interval_min))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        if hasattr(self, 'autosave_overwrite_chk') and self.autosave_overwrite_chk is not None:
                                            try:
                                                self.autosave_overwrite_chk.setChecked(bool(self.autosave_overwrite))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        if hasattr(self, 'autosave_rotate_chk') and self.autosave_rotate_chk is not None:
                                            try:
                                                self.autosave_rotate_chk.setChecked(bool(self.autosave_rotate))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        if hasattr(self, 'autosave_rotate_spin') and self.autosave_rotate_spin is not None:
                                            try:
                                                self.autosave_rotate_spin.setValue(int(self.autosave_rotate_count))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                        try:
                                            if hasattr(self, 'autosave_rotate_spin') and self.autosave_rotate_spin is not None:
                                                self.autosave_rotate_spin.setEnabled(bool(self.autosave_rotate))
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    app_loaded = True
                            except Exception:
                                # ignore failures reading auxiliary app settings
                                app_loaded = False

                        # 2) Fallback: if no app_settings.json, try an 'app_settings_last.json' snapshot
                        if not app_loaded:
                            try:
                                last_file = os.path.join(base_dir, 'app_settings_last.json')
                                if os.path.exists(last_file) and os.path.isfile(last_file):
                                    try:
                                        with open(last_file, 'r', encoding='utf-8') as lf:
                                            last_data = json.load(lf)
                                        if isinstance(last_data, dict) and 'canvas' in last_data:
                                            try:
                                                self.canvas.apply_settings(last_data['canvas'])
                                            except Exception as e:
                                                handle_exception(
                                                    ConfigurationError("Failed to apply fallback app-wide canvas settings", original_exception=e, path=last_file),
                                                    policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="warning", show_traceback=False),
                                                    parent=self,
                                                )
                                            try:
                                                self._app_canvas_settings_cache = dict(last_data['canvas']) if isinstance(last_data['canvas'], dict) else self._app_canvas_settings_cache
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                            # apply saved preferences from last snapshot if present
                                            try:
                                                lprefs = last_data.get('preferences', {}) or {}
                                                try:
                                                    self.muted = bool(lprefs.get('muted', getattr(self, 'muted', False)))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                try:
                                                    self.autosave_enabled = bool(lprefs.get('autosave_enabled', getattr(self, 'autosave_enabled', False)))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                try:
                                                    self.autosave_interval_min = int(lprefs.get('autosave_interval_min', getattr(self, 'autosave_interval_min', 5)))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                try:
                                                    self.autosave_overwrite = bool(lprefs.get('autosave_overwrite', getattr(self, 'autosave_overwrite', True)))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                try:
                                                    self.autosave_rotate = bool(lprefs.get('autosave_rotate', getattr(self, 'autosave_rotate', False)))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                try:
                                                    self.autosave_rotate_count = int(lprefs.get('autosave_rotate_count', getattr(self, 'autosave_rotate_count', 6)))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                # Normalize exclusivity
                                                try:
                                                    if self.autosave_rotate:
                                                        self.autosave_overwrite = False
                                                    elif self.autosave_overwrite:
                                                        self.autosave_rotate = False
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                # reflect to UI controls
                                                try:
                                                    if hasattr(self, 'mute_action') and self.mute_action is not None:
                                                        try:
                                                            self.mute_action.setChecked(bool(self.muted))
                                                        except Exception as e:
                                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                try:
                                                    if hasattr(self, 'autosave_chk') and self.autosave_chk is not None:
                                                        try:
                                                            self.autosave_chk.setChecked(bool(self.autosave_enabled))
                                                        except Exception as e:
                                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                    if hasattr(self, 'autosave_interval_spin') and self.autosave_interval_spin is not None:
                                                        try:
                                                            self.autosave_interval_spin.setValue(int(self.autosave_interval_min))
                                                        except Exception as e:
                                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                    if hasattr(self, 'autosave_overwrite_chk') and self.autosave_overwrite_chk is not None:
                                                        try:
                                                            self.autosave_overwrite_chk.setChecked(bool(self.autosave_overwrite))
                                                        except Exception as e:
                                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                    if hasattr(self, 'autosave_rotate_chk') and self.autosave_rotate_chk is not None:
                                                        try:
                                                            self.autosave_rotate_chk.setChecked(bool(self.autosave_rotate))
                                                        except Exception as e:
                                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                    if hasattr(self, 'autosave_rotate_spin') and self.autosave_rotate_spin is not None:
                                                        try:
                                                            self.autosave_rotate_spin.setValue(int(self.autosave_rotate_count))
                                                        except Exception as e:
                                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                    try:
                                                        if hasattr(self, 'autosave_rotate_spin') and self.autosave_rotate_spin is not None:
                                                            self.autosave_rotate_spin.setEnabled(bool(self.autosave_rotate))
                                                    except Exception as e:
                                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                            app_loaded = True
                                    except Exception:
                                        app_loaded = False
                            except Exception:
                                app_loaded = False

                        # 3) Fallback: if still not loaded, infer from last project
                        if not app_loaded:
                            # Try current project path first (if present in main settings)
                            try:
                                cp = getattr(self, 'current_project_path', None)
                                if cp and os.path.isfile(cp):
                                    try:
                                        with open(cp, 'r', encoding='utf-8') as pf:
                                            pdata = json.load(pf)
                                        if isinstance(pdata, dict):
                                            canv = (pdata.get('settings') or {}).get('canvas')
                                            if isinstance(canv, dict) and canv:
                                                try:
                                                    self.canvas.apply_settings(canv)
                                                except Exception as e:
                                                    handle_exception(
                                                        ConfigurationError("Failed to apply project canvas settings", original_exception=e, path=cp),
                                                        policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="warning", show_traceback=False),
                                                        parent=self,
                                                    )
                                                try:
                                                    self._app_canvas_settings_cache = dict(canv)
                                                except Exception as e:
                                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                                app_loaded = True
                                    except Exception:
                                        app_loaded = False
                            except Exception:
                                app_loaded = False

                        # 3) Final fallback: scan projects_home_path and pick most recently modified project
                        if not app_loaded:
                            try:
                                ph = getattr(self, 'projects_home_path', None)
                                if not ph:
                                    abd = getattr(self, 'app_base_dir', None)
                                    ph = os.path.join(abd, 'projects') if abd else os.getcwd()
                                best = None
                                best_mtime = 0
                                if os.path.isdir(ph):
                                    for fn in os.listdir(ph):
                                        if not str(fn).lower().endswith('.json'):
                                            continue
                                        pth = os.path.join(ph, fn)
                                        try:
                                            if not os.path.isfile(pth):
                                                continue
                                            with open(pth, 'r', encoding='utf-8') as f:
                                                d = json.load(f)
                                            if not isinstance(d, dict) or not isinstance(d.get('focuses', None), list):
                                                continue
                                            # must contain a canvas under settings to be useful
                                            canv = (d.get('settings') or {}).get('canvas')
                                            if not isinstance(canv, dict):
                                                continue
                                            mtime = os.path.getmtime(pth)
                                            if mtime > best_mtime:
                                                best_mtime = mtime
                                                best = canv
                                        except Exception:
                                            continue
                                if best and isinstance(best, dict):
                                    try:
                                        self.canvas.apply_settings(best)
                                    except Exception as e:
                                        handle_exception(
                                            ConfigurationError("Failed to apply inferred canvas settings", original_exception=e, path=ph),
                                            policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="warning", show_traceback=False),
                                            parent=self,
                                        )
                                    try:
                                        self._app_canvas_settings_cache = dict(best)
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    app_loaded = True
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # Ensure autosave timer follows any restored preference
                try:
                    if getattr(self, 'autosave_enabled', False):
                        try:
                            self._start_autosave_timer()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    else:
                        try:
                            self._stop_autosave_timer()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    logcfg = data.get('logging', {}) or {}
                    self.logging_enabled = bool(logcfg.get('enabled', False))
                    lvl = str(logcfg.get('level', 'INFO') or 'INFO')
                    self.logging_level = lvl
                    # file logging options
                    try:
                        lf = bool(logcfg.get('to_file', False))
                        fp = str(logcfg.get('file_path', '') or '')
                        self.logging_to_file = lf
                        if fp:
                            self.log_file_path = fp
                        # ensure file logging is active if requested
                        if lf:
                            try:
                                self.setup_file_logging(True, getattr(self, 'log_file_path', None))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    if self.logging_enabled:
                        try:
                            level_val = getattr(logging, lvl.upper(), logging.INFO)
                            logger.setLevel(level_val)
                            if not logger.handlers:
                                h = logging.StreamHandler()
                                h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
                                logger.addHandler(h)
                            # also setup file logging if requested by saved config
                            try:
                                if getattr(self, 'logging_to_file', False):
                                    try:
                                        self.setup_file_logging(True, getattr(self, 'log_file_path', None))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # Sync menu actions
                try:
                    self.frames_action.setChecked(bool(self.canvas.frames_enabled))
                    self.lineage_color_action.setChecked(bool(self.canvas.color_lines_by_lineage))
                    self.grid_action.setChecked(bool(getattr(self.canvas, '_grid_visible', True)))
                    # sync icon view toggle if present
                    if hasattr(self, 'icon_view_action'):
                        self.icon_view_action.setChecked(bool(getattr(self.canvas, 'icon_view_mode', False)))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # Start or stop autosave timer based on restored autosave_enabled setting
                try:
                    if getattr(self, 'autosave_enabled', False):
                        self._start_autosave_timer()
                    else:
                        self._stop_autosave_timer()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(
                    ConfigurationError("Failed to apply settings payload", original_exception=e, path=path),
                    policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True),
                    parent=self,
                )

    def detect_app_base_dir(self):
        """Detect a reasonable application base directory using platform conventions.

        Windows: use %LOCALAPPDATA%\\FocusTool or %APPDATA% if LOCALAPPDATA not available.
        *NIX: use XDG_DATA_HOME or ~/.local/share/focus_tool
        Falls back to current working directory.
        """
        try:
            if sys.platform.startswith('win'):
                base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
                if base:
                    self.app_base_dir = os.path.join(base, 'FocusTool')
                else:
                    # Prefer a location under the user's home when AppData is not available
                    self.app_base_dir = os.path.join(str(Path.home()), '.focus_tool')
            else:
                base = os.environ.get('XDG_DATA_HOME')
                if base:
                    self.app_base_dir = os.path.join(base, 'focus_tool')
                else:
                    self.app_base_dir = os.path.join(str(Path.home()), '.local', 'share', 'focus_tool')
        except Exception:
            # As a safe fallback use the user's home directory instead of cwd
            try:
                self.app_base_dir = os.path.join(str(Path.home()), '.focus_tool')
            except Exception:
                # last resort: fallback to cwd if even Path.home() fails
                self.app_base_dir = os.path.join(os.getcwd(), '.focus_tool')

    def ensure_app_dirs(self):
        """Create the standard subfolders under app_base_dir if they are missing.

        Subfolders: settings, projects, exports, library, icons, backups, logs
        """
        try:
            abd = getattr(self, 'app_base_dir', None)
            if not abd:
                # try to detect
                self.detect_app_base_dir()
                abd = getattr(self, 'app_base_dir', None)
            if not abd:
                return
            # normalize
            abd = os.path.abspath(abd)
            # Create the directory and ensure it's writable. If not writable, fall back to the user's home.
            try:
                os.makedirs(abd, exist_ok=True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # If abd is not writable, fall back to home-based location
            try:
                if not os.access(abd, os.W_OK):
                    home_abd = os.path.join(str(Path.home()), '.focus_tool')
                    os.makedirs(home_abd, exist_ok=True)
                    abd = os.path.abspath(home_abd)
            except Exception:
                # ignore and attempt to continue with abd
                pass
            subdirs = ['settings', 'projects', 'exports', 'library', 'icons', 'backups', 'logs']
            for sd in subdirs:
                try:
                    path = os.path.join(abd, sd)
                    os.makedirs(path, exist_ok=True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # update defaults for settings/database only if they appear uninitialized
            # or were left in obvious fallback locations (home or cwd). This ensures
            # that new installs get organized under the app_base_dir (AppData/XDG)
            # while preserving explicit user overrides.
            home = str(Path.home())
            cwd = os.path.abspath(os.getcwd())
            def _is_uninitialized_path(p: Optional[str]) -> bool:
                if not p:
                    return True
                try:
                    np = os.path.abspath(str(p))
                    # treat paths inside the user's home or the current working dir as
                    # uninitialized defaults that should be moved into app_base_dir
                    if np.startswith(home) or np.startswith(cwd):
                        return True
                except Exception:
                    return True
                return False

            if _is_uninitialized_path(getattr(self, 'settings_path', None)):
                self.settings_path = os.path.join(abd, 'settings', '.focus_tool_settings.json')
            if _is_uninitialized_path(getattr(self, 'database_path', None)):
                self.database_path = os.path.join(abd, 'settings', '.focus_tool_database.json')

            # if icon_library_path not set, prefer app icons folder
            if not getattr(self, 'icon_library_path', None):
                self.icon_library_path = os.path.join(abd, 'icons')
            # if projects_home_path not set or points to cwd, prefer app projects folder
            if not getattr(self, 'projects_home_path', None) or os.path.abspath(getattr(self, 'projects_home_path', '')) == cwd:
                self.projects_home_path = os.path.join(abd, 'projects')
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def export_all_states(self, output_dir: Optional[str] = None) -> tuple[int, int]:
        """Export all loaded states using the State Viewport's exporter.

        If output_dir is None, use the app's exports folder under the detected
        `app_base_dir` (optionally scoped by current project basename).

        Returns (success_count, failed_count).
        """
        try:
            sv = getattr(self, 'state_viewport_dock', None)
            if sv is None:
                return 0, 0
            # determine default output dir if not provided
            if not output_dir:
                abd = getattr(self, 'app_base_dir', None)
                if not abd:
                    try:
                        self.detect_app_base_dir()
                        abd = getattr(self, 'app_base_dir', None)
                    except Exception:
                        abd = None
                if not abd:
                    abd = os.path.join(str(Path.home()), '.focus_tool')
                exports_root = os.path.join(abd, 'exports')
                try:
                    if getattr(self, 'current_project_path', None):
                        proj_name = os.path.splitext(os.path.basename(self.current_project_path))[0]
                        output_dir = os.path.join(exports_root, proj_name)
                    else:
                        output_dir = os.path.join(exports_root, 'all_states')
                except Exception:
                    output_dir = exports_root
            try:
                os.makedirs(output_dir, exist_ok=True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return sv._export_states_to_dir(getattr(sv, '_state_meta', {}), output_dir)
        except Exception:
            try:
                logging.getLogger(__name__).exception('export_all_states failed')
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return 0, 0

    def save_settings(self):
        """Persist current settings to disk."""
        path = getattr(self, 'settings_path', None)
        if not path:
            return
        with operation_context("save_settings", module=__name__, file_path=path):
            # Sync runtime attributes back into the single source of truth
            try:
                if getattr(self, '_preferences', None) is None:
                    self._preferences = {}
                self._preferences['prefer_app_settings'] = bool(getattr(self, 'prefer_app_settings', False))
                self._preferences['muted'] = bool(getattr(self, 'muted', False))
                self._preferences['autosave_enabled'] = bool(getattr(self, 'autosave_enabled', False))
                self._preferences['autosave_interval_min'] = max(1, int(getattr(self, 'autosave_interval_min', 5) or 5))
                self._preferences['autosave_overwrite'] = bool(getattr(self, 'autosave_overwrite', True))
                self._preferences['autosave_rotate'] = bool(getattr(self, 'autosave_rotate', False))
                self._preferences['autosave_rotate_count'] = max(2, int(getattr(self, 'autosave_rotate_count', 6) or 6))
                try:
                    if self._preferences['autosave_rotate']:
                        self._preferences['autosave_overwrite'] = False
                    elif self._preferences['autosave_overwrite']:
                        self._preferences['autosave_rotate'] = False
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            data = {
                'app_version': getattr(self, 'app_version', '1.0.9'),
                'canvas': self.canvas.to_settings(),
                'keybindings': (self.keybinds.get_mapping() if getattr(self, 'keybinds', None) is not None else {}),
                'paths': {
                    'settings_path': getattr(self, 'settings_path', None),
                    'database_path': getattr(self, 'database_path', None),
                    'icon_library_path': getattr(self, 'icon_library_path', ''),
                    'projects_home_path': (getattr(self, 'projects_home_path', None)
                                           or (os.path.join(getattr(self, 'app_base_dir', os.getcwd()), 'projects')
                                               if getattr(self, 'app_base_dir', None) else os.getcwd())),
                    'app_base_dir': getattr(self, 'app_base_dir', None),
                    'current_project_path': getattr(self, 'current_project_path', None),
                },
                'preferences': dict(self._preferences) if getattr(self, '_preferences', None) is not None else {
                    'prefer_app_settings': bool(getattr(self, 'prefer_app_settings', False)),
                    'muted': bool(getattr(self, 'muted', False)),
                    'autosave_enabled': bool(getattr(self, 'autosave_enabled', False)),
                    'autosave_interval_min': int(getattr(self, 'autosave_interval_min', 5)),
                    'autosave_overwrite': bool(getattr(self, 'autosave_overwrite', True)),
                    'autosave_rotate': bool(getattr(self, 'autosave_rotate', False)),
                    'autosave_rotate_count': int(getattr(self, 'autosave_rotate_count', 6)),
                },
                'logging': {
                    'enabled': bool(getattr(self, 'logging_enabled', False)),
                    'level': str(getattr(self, 'logging_level', 'INFO')),
                    'to_file': bool(getattr(self, 'logging_to_file', False)) if hasattr(self, 'logging_to_file') else False,
                    'file_path': str(getattr(self, 'log_file_path', '') or ''),
                }
            }

            # Keep in-memory cache in sync so subsequent project loads can overlay the latest app settings
            try:
                self._app_canvas_settings_cache = dict(data['canvas']) if isinstance(data.get('canvas'), dict) else None
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            def _attempt_write(pth: str) -> bool:
                try:
                    ddir = os.path.dirname(pth)
                    if ddir:
                        os.makedirs(ddir, exist_ok=True)
                    with open(pth, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                    return True
                except Exception as e:
                    handle_exception(
                        FileOperationError("Failed to write settings", original_exception=e, path=pth),
                        policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True),
                        parent=self,
                    )
                    logger.warning('Failed to write settings to %s: %s', pth, e)
                    return False

        # Try saving; if it fails, present the user with actionable options
        if _attempt_write(path):
            # Also export a compact app_settings.json when the user prefers app-wide settings.
            try:
                try:
                    base_dir = os.path.dirname(path) or os.getcwd()
                except Exception:
                    base_dir = os.getcwd()
                app_settings_path = os.path.join(base_dir, 'app_settings.json')
                if bool(getattr(self, 'prefer_app_settings', False)):
                    # compact payload: only canvas and preferences (keep keybindings in main settings)
                    app_payload = {
                        'canvas': data.get('canvas', {}),
                        'preferences': data.get('preferences', {}),
                    }
                    try:
                        with open(app_settings_path, 'w', encoding='utf-8') as af:
                            json.dump(app_payload, af, indent=2)
                        # Also write a durable snapshot so the last-app-canvas can be
                        # restored even if the user later disables the toggle.
                        try:
                            last_path = os.path.join(base_dir, 'app_settings_last.json')
                            last_payload = {'canvas': data.get('canvas', {}), 'preferences': {'prefer_app_settings': False}}
                            with open(last_path, 'w', encoding='utf-8') as lf:
                                json.dump(last_payload, lf, indent=2)
                        except Exception:
                            logger.debug('Failed to write app_settings_last.json at %s', base_dir)
                    except Exception:
                        # non-fatal; log and continue
                        logger.debug('Failed to write auxiliary app_settings.json at %s', app_settings_path)
                else:
                    # If the user no longer prefers app-wide settings, remove any stale file
                    try:
                        if os.path.exists(app_settings_path):
                            os.remove(app_settings_path)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                try:
                    if hasattr(self, '_notify_save'):
                        try:
                            self._notify_save()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return True
            except Exception:
                return True

        # If we reach here the write failed — show a dialog with Retry / Choose Folder / Cancel
        try:
            while True:
                resp = QMessageBox.question(self, 'Save Settings Failed',
                                            f"Failed to write settings to:\n{path}\n\nChoose Retry to try again, Choose Folder to pick a different location, or Cancel to abort.",
                                            QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Ignore | QMessageBox.StandardButton.Cancel)
                # Map Ignore -> Choose Folder (QMessageBox uses Ignore for third option on some platforms)
                if resp == QMessageBox.StandardButton.Retry:
                    if _attempt_write(path):
                        try:
                            if hasattr(self, '_notify_save'):
                                try:
                                    self._notify_save()
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        return True
                        break
                    else:
                        continue
                else:
                    # Cancel pressed
                    break
        except Exception:
            # fallback: silently ignore after attempts
            pass

        return False

    # -----------------
    # Autosave helpers (placed on application window)
    # -----------------
    def _resolve_autosave_base_path(self) -> Optional[str]:
        """Determine the base path (without rotation suffix) for autosave files."""
        try:
            base_reference = getattr(self, 'current_project_path', None)
            if base_reference:
                directory = os.path.dirname(base_reference)
                base_name = os.path.basename(base_reference)
            else:
                # Sanitize tree_id to create a safe filename
                try:
                    import re
                    raw_tree_id = str(getattr(self, 'tree_id', '') or '')
                    safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', raw_tree_id).strip('_') or 'unsaved_focus_project'
                except Exception:
                    safe_tree_id = 'unsaved_focus_project'

                base_name = safe_tree_id
                if not base_name.lower().endswith('.json'):
                    base_name = f"{base_name}.json"
                directory = getattr(self, 'projects_home_path', None)
                if not directory:
                    abd = getattr(self, 'app_base_dir', None)
                    if abd:
                        directory = os.path.join(abd, 'projects')
                if not directory:
                    directory = os.getcwd()
            base_name = base_name.replace('\\', '_').replace('/', '_')
            autosave_dir = os.path.join(directory, 'autosaves')
            try:
                os.makedirs(autosave_dir, exist_ok=True)
            except Exception:
                logger.debug('Falling back to project directory for autosaves', exc_info=True)
                autosave_dir = directory
            return os.path.join(autosave_dir, base_name)
        except Exception as exc:
            logger.warning('Unable to resolve autosave path: %s', exc)
            return None

    def _start_autosave_timer(self):
        """Ensure the autosave timer is running according to user preferences."""
        try:
            if not bool(getattr(self, 'autosave_enabled', False)):
                self._stop_autosave_timer()
                return
            interval_min = int(getattr(self, 'autosave_interval_min', 5) or 5)
            interval_min = max(1, interval_min)
            interval_ms = interval_min * 60 * 1000
            timer = getattr(self, '_autosave_timer', None)
            if timer is None:
                timer = QTimer(self)
                timer.setSingleShot(False)
                try:
                    timer.timeout.connect(self._do_autosave)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self._autosave_timer = timer
            timer.setInterval(interval_ms)
            if not timer.isActive():
                timer.start()
        except Exception as exc:
            logger.warning('Failed to start autosave timer: %s', exc)

    def _stop_autosave_timer(self):
        """Stop the autosave timer if it is running."""
        try:
            timer = getattr(self, '_autosave_timer', None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as exc:
            logger.debug('Failed to stop autosave timer cleanly: %s', exc)

    def _do_autosave(self):
        """Perform an autosave using current preferences."""
        if not bool(getattr(self, 'autosave_enabled', False)):
            return
        if getattr(self, '_autosave_in_progress', False):
            return
        base_path = self._resolve_autosave_base_path()
        if not base_path:
            return
        try:
            payload = self._build_project_payload()
        except Exception as exc:
            logger.warning('Autosave aborted: failed to build project payload (%s)', exc)
            return
        if not payload:
            return
        self._autosave_in_progress = True
        try:
            if bool(getattr(self, 'autosave_rotate', False)):
                keep = int(getattr(self, 'autosave_rotate_count', 6) or 6)
                write_rotating_autosave(base_path, payload, keep)
            else:
                atomic_write_json(base_path, payload)
            try:
                self._last_autosave_path = base_path
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if hasattr(self, 'statusBar') and self.statusBar() is not None:
                    self.statusBar().showMessage(f"Auto-saved to {obfuscate_path(base_path)}", 2500)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self._flash_save_indicator(900)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as exc:
            logger.warning('Autosave failed for %s: %s', base_path, exc)
        finally:
            self._autosave_in_progress = False

    def _flash_save_indicator(self, duration_ms: int = 1200):
        """Briefly highlight the save indicator in the toolbar to show activity."""
        try:
            act = getattr(self, '_save_indicator_action', None)
            bright = getattr(self, '_save_icon_bright', None)
            dim = getattr(self, '_save_icon_dim', None)
            if act is None or bright is None or dim is None:
                return
            try:
                act.setIcon(bright)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # If we have the toolbar widget, apply the icon and a temporary style directly to the QToolButton
            btn = None
            try:
                tb = getattr(self, '_main_toolbar', None)
                if tb is not None:
                    try:
                        btn = tb.widgetForAction(act)
                        # Some themes may not immediately repaint, set icon on widget too
                        if btn is not None:
                            try:
                                btn.setIcon(bright)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            try:
                                # visible border/highlight to draw attention
                                btn.setStyleSheet('border:2px solid #44aa44; border-radius:4px;')
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception:
                        btn = None
            except Exception:
                btn = None
            # restore after duration
            def _restore():
                try:
                    act.setIcon(dim)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # restore widget icon/style as well
                try:
                    if btn is not None:
                        try:
                            btn.setIcon(dim)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        try:
                            btn.setStyleSheet('')
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                QTimer.singleShot(int(duration_ms), _restore)
            except Exception:
                _restore()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _notify_save(self):
        """Public helper to notify UI that a save occurred (status bar + flash)."""
        try:
            try:
                # status bar message if available
                if hasattr(self, 'statusBar') and self.statusBar() is not None:
                    sp = obfuscate_path(getattr(self, 'settings_path', '') or '')
                    self.statusBar().showMessage(f"Saved {sp}", 4000)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # flash the toolbar indicator
            try:
                self._flash_save_indicator()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def setup_file_logging(self, enable: bool, file_path: Optional[str] = None) -> None:
        """Enable or disable logging to a file. If file_path is None, use app_base_dir/logs/focus_tool.log"""
        try:
            # Keep attribute for persistence
            self.logging_to_file = bool(enable)
            if file_path:
                self.log_file_path = str(file_path)
            else:
                # default logs folder under app_base_dir
                abd = getattr(self, 'app_base_dir', None) or os.getcwd()
                logs_dir = os.path.join(abd, 'logs')
                try:
                    os.makedirs(logs_dir, exist_ok=True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.log_file_path = os.path.join(logs_dir, 'focus_tool.log')

            # remove existing file handler
            try:
                if getattr(self, '_file_log_handler', None):
                    logger.removeHandler(self._file_log_handler)
                    try:
                        self._file_log_handler.close()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self._file_log_handler = None
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            if enable:
                try:
                    # Use a rotating handler to prevent unbounded growth
                    from logging.handlers import RotatingFileHandler
                    fh = RotatingFileHandler(self.log_file_path, maxBytes=4 * 1024 * 1024, backupCount=4, encoding='utf-8')
                    lvl = getattr(logging, str(self.logging_level).upper(), logging.INFO)
                    fh.setLevel(lvl)
                    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
                    logger.addHandler(fh)
                    self._file_log_handler = fh
                except Exception:
                    # fallback to basic FileHandler
                    try:
                        fh = logging.FileHandler(self.log_file_path, encoding='utf-8')
                        lvl = getattr(logging, str(self.logging_level).upper(), logging.INFO)
                        fh.setLevel(lvl)
                        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
                        logger.addHandler(fh)
                        self._file_log_handler = fh
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                # not enabling file logging: nothing more to do
                pass
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def force_dump_logs(self) -> Optional[str]:
        """Write a diagnostic dump file (settings, project state) into the logs folder and return path."""
        try:
            abd = getattr(self, 'app_base_dir', None) or os.getcwd()
            logs_dir = os.path.join(abd, 'logs')
            try:
                os.makedirs(logs_dir, exist_ok=True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            ts = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
            fn = os.path.join(logs_dir, f'diagnostic_dump_{ts}.txt')
            info = {
                'timestamp_utc': ts,
                'app_version': getattr(self, 'app_version', None),
                'current_project_path': getattr(self, 'current_project_path', None),
                'settings_path': getattr(self, 'settings_path', None),
                'database_path': getattr(self, 'database_path', None),
                'logging': {
                    'enabled': bool(getattr(self, 'logging_enabled', False)),
                    'level': getattr(self, 'logging_level', 'INFO'),
                    'to_file': bool(getattr(self, 'logging_to_file', False)) if hasattr(self, 'logging_to_file') else False,
                    'file_path': getattr(self, 'log_file_path', None),
                },
                'canvas_settings': self.canvas.to_settings() if hasattr(self.canvas, 'to_settings') else {},
                'focus_count': len(getattr(self, 'focuses', [])),
                'library_count': len(getattr(self, 'library', {}) or {}),
            }
            with open(fn, 'w', encoding='utf-8') as f:
                f.write('Diagnostic dump generated by HOI4 Focus GUI\n')
                json.dump(info, f, indent=2, ensure_ascii=False)
                f.write('\n\n')
                f.write('Recent logger handlers:\n')
                f.write(str([type(h).__name__ for h in logger.handlers]))
                f.write('\n')
            # ensure logger records the dump
            try:
                logger.info('Diagnostic dump written to %s', fn)
                for h in logger.handlers:
                    try:
                        h.flush()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return fn
        except Exception:
            return None

    def load_database(self):
        """Load persistent library database from disk."""
        path = getattr(self, 'database_path', None)
        if not path:
            return
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if 'library' in data and isinstance(data['library'], dict):
                        self.library = data['library']
                    if 'folders' in data and isinstance(data['folders'], dict):
                        self.library_folders = data['folders']
                    if 'icon_library' in data and isinstance(data['icon_library'], dict):
                        self.icon_library = data['icon_library']
                        try:
                            logger.debug("Loaded %d icons from database", len(self.icon_library))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # Refresh UI to show loaded data
                    self.refresh_library_list()
        except Exception as e:
            logger.warning("Could not load library database: %s", e)

    def save_database(self):
        """Persist library database to disk automatically."""
        path = getattr(self, 'database_path', None)
        if not path:
            return
        try:
            data = {
                'library': self.library,
                'folders': self.library_folders,
                'icon_library': self.icon_library,
                'version': getattr(self, 'app_version', '1.0.9')
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Could not save library database: %s", e)

    def closeEvent(self, event):
        try:
            self.save_settings()
            self.save_database()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return super().closeEvent(event)

    # -------------------------
    # Bulk actions and arrangement
    # -------------------------
    def delete_selected_focuses(self):
        selected = [it for it in self.canvas.selectedItems() if isinstance(it, FocusNode)]
        if not selected:
            QMessageBox.information(self, "Delete", "No focuses selected.")
            return
            reply = QMessageBox.question(self, "Delete Selected", f"Delete {len(selected)} selected focus(es)? This removes connections too.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         defaultButton=QMessageBox.StandardButton.Yes)
            if reply != QMessageBox.StandardButton.Yes:
                return
        for node in list(selected):
            self.delete_focus_node(node)
        try:
            self.canvas.clearSelection()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_status()

    def delete_selected_items(self, confirm: bool = True):
        """Unified deletion: deletes any selected FocusNode, EventNode, or NoteNode in one action.

        This method is called by the view when Delete/Backspace is pressed so the shortcut is
        unambiguous and removes items in a single confirmation flow.
        """
        # Collect different types
        selected = list(self.canvas.selectedItems())
        if not selected:
            return
        focuses = [it for it in selected if isinstance(it, FocusNode)]
        events = [it for it in selected if isinstance(it, EventNode)]
        notes = [it for it in selected if isinstance(it, NoteNode)]

        total = len(focuses) + len(events) + len(notes)
        if total == 0:
            return
        # Confirm only if requested by the caller (Delete key or explicit action). Backspace may pass confirm=False.
        if confirm:
            reply = QMessageBox.question(self, "Delete Selected",
                                         f"Delete {total} selected item(s)? This will remove connections and cannot be undone.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         defaultButton=QMessageBox.StandardButton.Yes)
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Delete focuses via existing helper to ensure proper cleanup
        try:
            for node in list(focuses):
                try:
                    self.delete_focus_node(node)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Delete events
        try:
            for node in list(events):
                try:
                    # bulk delete already confirmed — suppress per-event dialogs
                    self.delete_event_node(node, confirm=False)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Delete notes
        try:
            for note in list(notes):
                try:
                    note._delete_self()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        try:
            self.canvas.clearSelection()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_status()

    def clear_library_database(self):
        """Clear the persisted library database on disk and in-memory entries.

        Prompts the user for confirmation. If confirmed, deletes the database file
        (if present), clears the in-memory library and folder metadata, refreshes
        the UI and saves an empty database file.
        """
        reply = QMessageBox.question(self, "Clear Library Database",
                                     "This will permanently delete the on-disk library database and clear all library entries. Continue?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        path = getattr(self, 'database_path', None)
        try:
            # Remove file if it exists
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.warning("could not remove database file: %s", e)
            # Clear in-memory structures
            self.library = {}
            self.library_folders = {}
            # Refresh UI and persist empty DB
            try:
                self.refresh_library_list()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.save_database()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.statusBar().showMessage("Library database cleared.", 5000)
        except Exception as e:
            show_error(self, "Error", "Could not clear library database.", exc=e)

    def arrange_selected_vertically(self):
        """Align selected focus nodes into a vertical line with consistent spacing."""
        nodes = [it for it in self.canvas.selectedItems() if isinstance(it, FocusNode)]
        if not nodes:
            QMessageBox.information(self, "Arrange", "Select some focus nodes first.")
            return
        # Determine base x from median of current x positions
        xs = sorted([n.focus.x for n in nodes])
        base_x = xs[len(xs)//2]
        # Sort nodes by y then id for stable order
        nodes.sort(key=lambda n: (n.focus.y, n.focus.id))
        for i, n in enumerate(nodes):
            n.focus.x = base_x
            n.focus.y = nodes[0].focus.y + i
            n.setPos(n.focus.x * GRID_UNIT, n.focus.y * GRID_UNIT)
            n.update_connections()
        try:
            self.canvas.schedule_frame_update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage(f"Arranged {len(nodes)} node(s) vertically")

    def arrange_all_by_root_vertically(self):
        """Arrange all nodes into vertical columns by lineage/root, preserving prerequisite order as much as possible."""
        canvas = self.canvas
        # Recompute lineage groups
        try:
            canvas.recompute_lineages()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Group nodes by lineage root id
        groups: Dict[str, List[FocusNode]] = {}
        for nid, node in canvas.nodes.items():
            lid = canvas._lineage_of_node.get(nid, nid)
            groups.setdefault(lid, []).append(node)
        # Assign columns left to right by sorted root id
        col_x = 0
        for idx, (lid, nodes) in enumerate(sorted(groups.items(), key=lambda kv: kv[0])):
            # Sort within column by y then by id
            nodes.sort(key=lambda n: (n.focus.y, n.focus.id))
            for i, n in enumerate(nodes):
                n.focus.x = col_x
                n.focus.y = i
                n.setPos(n.focus.x * GRID_UNIT, n.focus.y * GRID_UNIT)
                n.update_connections()
            col_x += 2  # gap between columns
        try:
            canvas.schedule_frame_update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage("Arranged all nodes vertically by root")

    # -------------------------
    # Append/grow without clearing
    # -------------------------
    def show_append_dialog(self):
        """Dialog to append newly generated nodes into the existing canvas without clearing."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Append Generated Nodes")
        form = QFormLayout(dialog)
        node_count_spin = QSpinBox(); node_count_spin.setRange(1, 2000); node_count_spin.setValue(40)
        root_count_spin = QSpinBox(); root_count_spin.setRange(1, 8); root_count_spin.setValue(1)
        max_depth_spin = QSpinBox(); max_depth_spin.setRange(1, 12); max_depth_spin.setValue(6)
        branching_min = QSpinBox(); branching_min.setRange(1, 6); branching_min.setValue(1)
        branching_max = QSpinBox(); branching_max.setRange(1, 6); branching_max.setValue(2)
        layout_rand = QSlider(Qt.Orientation.Horizontal); layout_rand.setRange(0, 100); layout_rand.setValue(35)
        seed_edit = QLineEdit(""); seed_edit.setPlaceholderText("random if empty")
        use_theme_cb = QCheckBox("Use theme names"); use_theme_cb.setChecked(True)
        use_lib_cb = QCheckBox("Use library names"); use_lib_cb.setChecked(True)
        offset_x_spin = QSpinBox(); offset_x_spin.setRange(-10000, 10000); offset_x_spin.setValue(0)
        offset_y_spin = QSpinBox(); offset_y_spin.setRange(-10000, 10000); offset_y_spin.setValue(0)
        auto_place_cb = QCheckBox("Auto place to the right of current tree"); auto_place_cb.setChecked(True)
        form.addRow("Target node count:", node_count_spin)
        form.addRow("Root count:", root_count_spin)
        form.addRow("Max depth:", max_depth_spin)
        form.addRow("Min branch:", branching_min)
        form.addRow("Max branch:", branching_max)
        form.addRow("Layout randomness:", layout_rand)
        form.addRow("Seed:", seed_edit)
        form.addRow(use_theme_cb)
        form.addRow(use_lib_cb)
        form.addRow(auto_place_cb)
        form.addRow("Manual X offset (grid):", offset_x_spin)
        form.addRow("Manual Y offset (grid):", offset_y_spin)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        params = {
            'node_count': node_count_spin.value(),
            'root_count': root_count_spin.value(),
            'max_depth': max_depth_spin.value(),
            'branching': (branching_min.value(), branching_max.value()),
            'layout_randomness': layout_rand.value()/100.0,
            'seed': int(seed_edit.text()) if seed_edit.text().strip().isdigit() else None,
            'use_theme_names': use_theme_cb.isChecked(),
            'use_library_names': use_lib_cb.isChecked(),
            'auto_place': auto_place_cb.isChecked(),
            'offset_x': offset_x_spin.value(),
            'offset_y': offset_y_spin.value()
        }
        self.append_generated_nodes(params)

    def append_generated_nodes(self, params: Dict[str, Any]):
        """Generate additional focuses and add them to the existing project without clearing anything."""
        try:
            from _focusGenerator import FocusTreeGenerator
        except Exception as e:
            show_error(self, "Generator", "Cannot import generator.", exc=e)
            return
        # Determine placement offsets
        ox = int(params.get('offset_x', 0))
        oy = int(params.get('offset_y', 0))
        if params.get('auto_place', True) and self.canvas.nodes:
            # place to the right of current max x + gap
            max_x = max(n.focus.x for n in self.canvas.nodes.values())
            ox = max_x + 3
        # Build generator with existing library/theme
        gen = FocusTreeGenerator(library=self.library, country_tag=self.country_tag, id_prefix=self.tree_id, theme=self.theme_data)
        try:
            part = gen.generate(
                tree_id=f"{self.tree_id}_append",
                root_count=params.get('root_count', 1),
                max_depth=params.get('max_depth', 6),
                branching=params.get('branching', (1,2)),
                use_library_names=params.get('use_library_names', True),
                use_theme_names=params.get('use_theme_names', True),
                seed=params.get('seed', None),
                node_count=params.get('node_count', 40),
                branch_density=5.0,
                start_x=ox,
                start_y=oy,
                layout_randomness=float(params.get('layout_randomness', 0.35)),
                theme=self.theme_data if self.theme_data else None
            )
        except Exception as e:
            show_error(self, "Generate", "Append generation failed.", exc=e)
            return
        # Add to current canvas and wire connections
        added_count = 0
        main_uw = getattr(self, 'undo_stack', None)
        macro = None
        if main_uw is not None:
            macro = MacroCommand(description=f"Append Generated {len(part)} focuses")
        for f in part:
            # avoid id collision
            if any(existing.id == f.id for existing in self.focuses):
                # make unique
                base = f.id
                i = 1
                nid = f"{base}_{i}"
                while any(ex.id == nid for ex in self.focuses):
                    i += 1
                    nid = f"{base}_{i}"
                f.id = nid
            if macro is not None:
                macro.addCommand(AddFocusCommand(self, f, description=f"Add generated focus {f.id}"))
            else:
                try:
                    f.mutually_exclusive = []
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.focuses.append(f)
                self.canvas.add_focus_node(f)
            added_count += 1
        # create connections (use create connection commands if macro)
        for f in part:
            for p in getattr(f, 'prerequisites', []) or []:
                if p in self.canvas.nodes:
                    if macro is not None:
                        macro.addCommand(CreateConnectionCommand(self.canvas, p, f.id))
                    else:
                        self.canvas.create_connection(p, f.id)
        # Update frames/colors and status
        try:
            # Expand palette if needed
            nets = [getattr(f, 'network_id', None) for f in self.focuses if getattr(f, 'network_id', None) is not None]
            nets = nets or [0]
            self.canvas.compute_palette_for_networks(nets)
            self.canvas.update_frames()
            self.canvas.refresh_connection_colors()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # push macro to undo stack if present
        if macro is not None and getattr(self, 'undo_stack', None) is not None:
            try:
                self.undo_stack.push(macro)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage(f"Appended {added_count} generated focus(es)")
        self.update_status()
        try:
            # Schedule reflow of isolated nodes (guarded)
            if getattr(self.canvas, 'auto_layout_enabled', False):
                if getattr(self.canvas, '_layout_in_progress', False):
                    pass
                else:
                    if getattr(self.canvas, 'auto_layout_enabled', False):
                        if not self.canvas._reflow_timer.isActive():
                            self.canvas._reflow_timer.start()
        except Exception:
            try:
                if getattr(self.canvas, 'auto_layout_enabled', False):
                    if getattr(self.canvas, 'auto_layout_enabled', False):
                        self.canvas.reflow_unconnected_nodes()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def show_network_colors_dialog(self):
        """Dialog to let user customize colors for known networks."""
        # collect known network ids from current canvas
        canvas = self.canvas
        known = sorted(k for k in canvas.network_colors.keys())
        # create dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Network Colors")
        layout = QFormLayout(dlg)
        editors: Dict[int, QPushButton] = {}
        for net in known:
            btn = QPushButton()
            col = canvas.network_colors.get(net, QColor(Qt.GlobalColor.blue))
            pix = QPixmap(24, 24)
            pix.fill(col)
            btn.setIcon(QIcon(pix))
            def make_clicked(n):
                def _():
                    current = canvas.network_colors.get(n, QColor(Qt.GlobalColor.blue))
                    new = QColorDialog.getColor(current, self, f"Pick color for network {n}")
                    if new.isValid():
                        canvas.network_colors[n] = new
                        pix = QPixmap(24, 24)
                        pix.fill(new)
                        btn.setIcon(QIcon(pix))
                return _
            btn.clicked.connect(make_clicked(net))
            layout.addRow(f"Network {net}", btn)
            editors[net] = btn
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addRow(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # refresh visuals
            try:
                canvas.update_frames()
                canvas.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # -------------------------
    # Status / view helpers
    # -------------------------
    def update_status(self):
        """Update status bar information"""
        self.focus_count_label.setText(f"Focuses: {len(self.focuses)}")
        zoom = int(self.view.transform().m11() * 100)
        self.zoom_label.setText(f"{zoom}%")

    # -------- Node Palette and creation helpers --------
    def _mouse_scene_pos_or_center(self) -> QPointF:
        try:
            return self.view.mapToScene(self.view.mapFromGlobal(QCursor.pos()))
        except Exception:
            return QPointF(0, 0)

    def open_node_palette(self) -> None:
        """Open a grid menu to choose node type; create at mouse position and allow drag-drop."""
        try:
            dlg = NodePaletteDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selection:
                return
            kind = dlg.selection
            pos = self._mouse_scene_pos_or_center()
            if kind == 'focus':
                self._create_focus_at(pos)
            elif kind == 'event':
                self._create_event_at(pos)
            elif kind == 'note':
                self._create_note_at(pos)
        except Exception as e:
            show_error(self, 'New Node', 'Failed to create node.', exc=e)

    def _create_focus_at(self, scene_pos: QPointF) -> None:
        # derive grid coords
        gx = int(round(scene_pos.x() / GRID_UNIT))
        gy = int(round(scene_pos.y() / GRID_UNIT))
        # find unique id
        base = 'focus'
        i = 1
        new_id = f'{base}_{i}'
        existing = {f.id for f in self.focuses}
        # Ensure we check the prefixed candidate when ensuring uniqueness so numbering
        # increments correctly for the project tag (e.g. TAG_focus_1 -> TAG_focus_2)
        pref_id = self._prefix_focus_id(new_id)
        while pref_id in existing or pref_id in getattr(self.canvas, 'nodes', {}):
            i += 1
            new_id = f'{base}_{i}'
            pref_id = self._prefix_focus_id(new_id)
        f = Focus(id=pref_id, name='', x=gx, y=gy)
        uw = getattr(self, 'undo_stack', None)
        if uw is not None:
            uw.push(AddFocusCommand(self, f, description=f"Add {new_id}"))
        else:
            self.focuses.append(f)
            self.canvas.add_focus_node(f)
        self.statusBar().showMessage(f"Added focus: {new_id}")

    def _create_event_at(self, scene_pos: QPointF) -> None:
        gx = int(round(scene_pos.x() / GRID_UNIT))
        gy = int(round(scene_pos.y() / GRID_UNIT))
        base = f"{self.tree_id}.event"
        eid = base
        i = 1
        exists = {e.id for e in self.events}
        while eid in exists:
            eid = f"{base}.{i}"; i += 1
        ev = Event(id=eid, title='New Event', x=gx, y=gy)
        self.events.append(ev)
        self.canvas.add_event_node(ev)
        self.edit_event(ev)

    def _create_note_at(self, scene_pos: QPointF) -> None:
        # ensure notes system enabled
        try:
            if not getattr(self.canvas, 'notes_enabled', False) and hasattr(self, 'notes_toggle_action'):
                self.notes_toggle_action.setChecked(True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.canvas.add_note('Note', scene_pos)

    def zoom_in(self):
        """Zoom in on the view"""
        # Center zoom on current mouse if possible for consistency with wheel
        try:
            vp = self.view.mapFromGlobal(QCursor.pos())
            scene_before = self.view.mapToScene(vp)
            # respect zoom limits
            cur = self.view.transform().m11()
            if cur >= getattr(self.view, 'MAX_SCALE', 8.0):
                return
            self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
            # compute allowed factor
            factor = 1.15
            if cur * factor > getattr(self.view, 'MAX_SCALE', 8.0):
                factor = getattr(self.view, 'MAX_SCALE', 8.0) / cur
            self.view.scale(factor, factor)
            scene_after = self.view.mapToScene(vp)
            off = scene_after - scene_before
            self.view.translate(off.x(), off.y())
        except Exception:
            try:
                cur = self.view.transform().m11()
                if cur < getattr(self.view, 'MAX_SCALE', 8.0):
                    self.view.scale(1.15, 1.15)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_status()
        try:
            if getattr(self.view, '_zoom_overlay', None) is not None:
                self.view._zoom_overlay.setText(f"{int(self.view.transform().m11()*100)}%")
                self.view._zoom_overlay.adjustSize()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def zoom_out(self):
        """Zoom out of the view"""
        try:
            vp = self.view.mapFromGlobal(QCursor.pos())
            scene_before = self.view.mapToScene(vp)
            cur = self.view.transform().m11()
            if cur <= getattr(self.view, 'MIN_SCALE', 0.05):
                return
            self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
            factor = 1.0/1.15
            if cur * factor < getattr(self.view, 'MIN_SCALE', 0.05):
                factor = getattr(self.view, 'MIN_SCALE', 0.05) / cur
            self.view.scale(factor, factor)
            scene_after = self.view.mapToScene(vp)
            off = scene_after - scene_before
            self.view.translate(off.x(), off.y())
        except Exception:
            try:
                cur = self.view.transform().m11()
                if cur > getattr(self.view, 'MIN_SCALE', 0.05):
                    self.view.scale(1.0/1.15, 1.0/1.15)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_status()
        try:
            if getattr(self.view, '_zoom_overlay', None) is not None:
                self.view._zoom_overlay.setText(f"{int(self.view.transform().m11()*100)}%")
                self.view._zoom_overlay.adjustSize()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def fit_view(self):
        """Fit all focuses in view"""
        # Compute bounding rect only from focus nodes to avoid fitting the entire grid
        if self.focuses and self.canvas.nodes:
            try:
                rect = None
                for node in self.canvas.nodes.values():
                    nb = node.sceneBoundingRect()
                    if rect is None:
                        rect = QRectF(nb)
                    else:
                        rect = rect.united(nb)
                if rect is None or rect.isNull():
                    # Fallback to reset
                    self.view.resetTransform()
                else:
                    # Add a small margin
                    margin = max(rect.width(), rect.height()) * 0.1
                    rect.adjust(-margin, -margin, margin, margin)
                    # Prevent absurdly large rects from weird values
                    if rect.width() > 100000 or rect.height() > 100000:
                        self.view.resetTransform()
                    else:
                        self.view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            except Exception as e:
                logger.exception("[view] fit_view error")
                self.view.resetTransform()
        else:
            self.view.resetTransform()
        self.update_status()

    # -------------------------
    # Focus CRUD
    # -------------------------
    def add_focus(self):
        """Add a new focus with enhanced dialog"""
        dialog = QInputDialog()
        dialog.setWindowTitle("New Focus")
        dialog.setLabelText("Enter focus ID:")
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            focus_id = dialog.textValue().strip()
            if focus_id:
                # Check for duplicate
                if any(f.id == focus_id for f in self.focuses):
                    QMessageBox.warning(self, "Error", "Focus ID already exists!")
                    return
                # Ensure focus id is prefixed with project tag
                # Ensure we compare and create using the prefixed id so user-entered ids
                # without the tag get normalized and checked against existing prefixed ids.
                pref = self._prefix_focus_id(focus_id)
                if any(f.id == pref for f in self.focuses):
                    QMessageBox.warning(self, "Error", "Focus ID already exists!")
                    return
                focus = Focus(id=pref)
                # Position new focus at center of view or next to last focus
                if self.focuses:
                    last_focus = self.focuses[-1]
                    focus.x = last_focus.x + 1
                    focus.y = last_focus.y
                else:
                    focus.x = 0
                    focus.y = 0
                # Use undo stack if available
                uw = getattr(self, 'undo_stack', None)
                if uw is not None:
                    cmd = AddFocusCommand(self, focus)
                    uw.push(cmd)
                else:
                    try:
                        focus.mutually_exclusive = []
                    except Exception:
                        try:
                            focus.mutually_exclusive = []
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.focuses.append(focus)
                    self.canvas.add_focus_node(focus)
                    try:
                        self.update_status()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.statusBar().showMessage(f"Added focus: {focus_id}")

    def edit_focus(self, focus):
        """Edit focus properties with enhanced dialog"""
        # snapshot before
        before = clone_focus_pure(focus)
        # Highlight lineage while editing
        try:
            self.canvas.highlight_lineage(focus.id)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        dialog = FocusEditDialog(focus, self, library=self.library)
        # Expose the dialog so canvas.add_event_focus_link can refresh its reward_edit
        # when injecting a concise country_event snippet. Ensure we always clear the
        # reference after the dialog closes to avoid stale pointers.
        try:
            try:
                self.active_focus_dialog = dialog
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            result = dialog.exec()
        finally:
            try:
                if hasattr(self, 'active_focus_dialog'):
                    try:
                        delattr(self, 'active_focus_dialog')
                    except Exception:
                        try:
                            del self.active_focus_dialog
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Clear highlight after editing ends
        try:
            self.canvas.clear_highlight()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        if result == QDialog.DialogCode.Accepted:
            # After dialog, create an EditFocusCommand capturing before and after
            after = clone_focus_pure(focus)
            uw = getattr(self, 'undo_stack', None)
            if uw is not None:
                cmd = EditFocusCommand(self, before, after, description=f"Edit Focus {before.id}")
                uw.push(cmd)
            else:
                # apply id change mapping if necessary
                old_id = before.id
                if old_id != focus.id:
                    if old_id in self.canvas.nodes:
                        node = self.canvas.nodes[old_id]
                        del self.canvas.nodes[old_id]
                        self.canvas.nodes[focus.id] = node
                    for f in self.focuses:
                        f.prerequisites = [focus.id if p == old_id else p for p in f.prerequisites]
                        f.mutually_exclusive = [focus.id if m == old_id else m for m in f.mutually_exclusive]
                try:
                    self.canvas.refresh_mutex_connectors()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # -------------------------
    # Event CRUD
    # -------------------------
    def edit_event(self, event: Event):
        """Open the EventEditDialog and apply changes; update canvas mapping if id changes."""
        before_id = event.id
        dlg = EventEditDialog(event, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                # if id changed, update canvas mapping
                if before_id != event.id and hasattr(self.canvas, 'event_nodes'):
                    try:
                        node = self.canvas.event_nodes.get(before_id)
                        if node:
                            del self.canvas.event_nodes[before_id]
                            self.canvas.event_nodes[event.id] = node
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.statusBar().showMessage(f"Edited event: {event.id}")

    def delete_event_node(self, node: EventNode, confirm: bool = True):
        try:
            eid = getattr(node, 'event', None).id if getattr(node, 'event', None) else None
        except Exception:
            eid = None
        # optional confirmation (caller may have already confirmed)
        if confirm:
            reply = QMessageBox.question(self, "Delete Event", f"Delete event '{eid or '(unknown)'}' and its links?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         defaultButton=QMessageBox.StandardButton.Yes)
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            # remove from list
            self.events = [e for e in self.events if getattr(e, 'id', None) != eid]
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            # remove all cross-type links for this event (notes, focuses, events)
            if hasattr(self.canvas, 'remove_links_for'):
                self.canvas.remove_links_for(node)
            else:
                # fallback to specific removals if generic is unavailable
                if hasattr(self.canvas, 'remove_note_event_links_for'):
                    self.canvas.remove_note_event_links_for(node)
                if hasattr(self.canvas, 'remove_event_focus_links_for'):
                    self.canvas.remove_event_focus_links_for(node)
                if hasattr(self.canvas, 'remove_event_event_links_for'):
                    self.canvas.remove_event_event_links_for(node)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            # remove from canvas
            if hasattr(self.canvas, 'event_nodes') and eid in self.canvas.event_nodes:
                try:
                    del self.canvas.event_nodes[eid]
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.canvas.removeItem(node)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage(f"Deleted event: {eid}")

    # -------------------------
    # Unified Smart Linker
    # -------------------------
    def link_selected_chain_smart(self) -> None:
        """Create appropriate connectors for the current selection in order.

        Rules:
        - Accepts selection of `NoteNode`, `EventNode`, `FocusNode` mixed.
        - Filters to supported item types and preserves selection order.
        - Links items pairwise in a chain: items[i] → items[i+1].
        - Uses canvas.add_link to pick the right connector type per pair.
        """
        try:
            items = [it for it in self.canvas.selectedItems() if isinstance(it, (NoteNode, EventNode, FocusNode))]
            # If nothing selected, simply enter connection mode and inform the user via status bar
            if len(items) == 0:
                try:
                    self.canvas.connection_mode = True
                    if hasattr(self, 'connect_action'):
                        try:
                            self.connect_action.setChecked(True)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.statusBar().showMessage("Connection mode: Click a node to start linking.")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return

            # If exactly one item selected, start a connection from it (no modal dialogs)
            if len(items) == 1:
                src = items[0]
                try:
                    self.canvas.connection_mode = True
                    if hasattr(self, 'connect_action'):
                        try:
                            self.connect_action.setChecked(True)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # set the canvas connection_start so the next click will complete the link
                    self.canvas.connection_start = src
                    try:
                        src.setSelected(True)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.statusBar().showMessage("Connection mode: Click target node to complete link.")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return

            # Multiple items selected: link pairwise in selection order without any modal UI
            created = 0
            for i in range(len(items) - 1):
                try:
                    conn = self.canvas.add_link(items[i], items[i+1])
                    if conn is not None:
                        created += 1
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            if created > 0:
                self.statusBar().showMessage(f"Created {created} link(s).")
            else:
                # no modal dialogs — just update status bar
                self.statusBar().showMessage("No valid links created from selection.")
        except Exception:
            # Swallow detailed modal error reporting to avoid popups; log to status bar
            try:
                self.statusBar().showMessage("Failed to create links from selection (see log).")
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def copy_selected_focuses(self):
        """Copy selected focus nodes into an internal buffer (simple dict list)."""
        selected = [n for n in self.canvas.nodes.values() if n.isSelected()]
        if not selected:
            QMessageBox.information(self, "Copy", "No focuses selected to copy.")
            return
        buf = []
        for node in selected:
            f = node.focus
            buf.append({
                'id': f.id,
                'name': f.name,
                'cost': f.cost,
                'description': f.description,
                'prerequisites': list(f.prerequisites),
                'mutually_exclusive': list(f.mutually_exclusive),
                'available': f.available,
                'bypass': f.bypass,
                'completion_reward': f.completion_reward,
                'ai_will_do': f.ai_will_do,
                'x': int(node.x() / GRID_UNIT),
                'y': int(node.y() / GRID_UNIT),
            })
        self._copy_buffer = buf
        try:
            self.statusBar().showMessage(f"Copied {len(buf)} focus(es) to buffer.")
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def paste_focuses(self):
        """Paste focuses from internal buffer with offset to avoid overlap."""
        buf = getattr(self, '_copy_buffer', None)
        if not buf:
            QMessageBox.information(self, "Paste", "Copy buffer is empty.")
            return
        # Determine paste origin: use current mouse scene position if available, else offset by 1 grid unit
        view = getattr(self, 'view', None)
        try:
            if view is not None:
                cursor_pos = view.mapToScene(view.mapFromGlobal(QCursor.pos()))
                origin_x = int(round(cursor_pos.x() / GRID_UNIT))
                origin_y = int(round(cursor_pos.y() / GRID_UNIT))
            else:
                raise Exception()
        except Exception:
            # fallback: offset by 1 unit from first copied item positions
            origin_x = None
            origin_y = None

        # Build mapping for old_id -> new_id so we can remap prereqs
        existing_ids = {f.id for f in self.focuses}
        node_map = {}  # old_id -> new Focus
        pasted_nodes = []

        # First pass: create Focus objects with remapped ids and provisional positions
        main_uw = getattr(self, 'undo_stack', None)
        paste_macro = None
        if main_uw is not None:
            paste_macro = MacroCommand(description=f"Paste {len(buf)} focuses")
        for i, entry in enumerate(buf):
            base_id = entry.get('id', f'focus_copy_{i}')
            new_id = base_id
            suffix = 1
            # Ensure pasted ids are checked against the prefixed form so they don't
            # accidentally collide with existing prefixed focuses.
            pref_new = self._prefix_focus_id(new_id)
            while pref_new in existing_ids or pref_new in node_map:
                new_id = f"{base_id}_{suffix}"
                suffix += 1
                pref_new = self._prefix_focus_id(new_id)
            # position: if origin provided, translate relative to the first entry's coords
            if origin_x is not None and origin_y is not None:
                # compute relative offset from first buffer entry
                first_x = buf[0].get('x', 0)
                first_y = buf[0].get('y', 0)
                rel_x = entry.get('x', 0) - first_x
                rel_y = entry.get('y', 0) - first_y
                x = origin_x + rel_x
                y = origin_y + rel_y
            else:
                x = entry.get('x', 0) + 1
                y = entry.get('y', 0) + 1

            # Store the focus using the prefixed id when adding to this project
            fobj = Focus(
                id=self._prefix_focus_id(new_id),
                name=entry.get('name', ''),
                cost=entry.get('cost', 10),
                description=entry.get('description', ''),
                icon=entry.get('icon', None),
                prerequisites=[],  # fill in second pass
                mutually_exclusive=[],
                available=entry.get('available', ''),
                bypass=entry.get('bypass', ''),
                completion_reward=entry.get('completion_reward', ''),
                ai_will_do=entry.get('ai_will_do', 1),
                x=x,
                y=y,
            )
            node_map[entry.get('id')] = fobj
            existing_ids.add(new_id)
            if paste_macro is not None:
                try:
                    fobj._copied = True
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                paste_macro.addCommand(AddFocusCommand(self, fobj, description=f"Pasted focus {fobj.id}"))
                # node will be created by the AddFocusCommand on redo
                node = None
            else:
                try:
                    fobj._copied = True
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.focuses.append(fobj)
                QTimer.singleShot(0, self.fit_view)
                try:
                    restyle = lambda: (
                        getattr(getattr(self, 'canvas', None), 'rebuild_connection_styles', None)
                        or getattr(getattr(self, 'canvas', None), '_apply_prereq_group_styles', None)
                        or getattr(self, '_apply_prereq_group_styles', lambda: None)
                    )()
                    QTimer.singleShot(50, restyle)
                    try:
                        QTimer.singleShot(250, restyle)
                        QTimer.singleShot(1000, restyle)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            pasted_nodes.append((entry.get('id'), fobj, node))

        # Second pass: remap prerequisites/mutex and create connections between pasted nodes
        orig_to_new = {orig: new.id for orig, new, _ in pasted_nodes}
        for orig, fobj, node in pasted_nodes:
            # remap prerequisites: if prereq referenced a copied node, remap to new id
            old_prereqs = next((e for e in buf if e.get('id') == orig), None)
            old_prs = old_prereqs.get('prerequisites', []) if old_prereqs else []
            new_prs = []
            for p in old_prs:
                if p in orig_to_new:
                    new_prs.append(orig_to_new[p])
                else:
                    new_prs.append(p)
            fobj.prerequisites = new_prs
            # mutually exclusive
            old_mutex = old_prereqs.get('mutually_exclusive', []) if old_prereqs else []
            new_mutex = []
            for m in old_mutex:
                if m in orig_to_new:
                    new_mutex.append(orig_to_new[m])
                else:
                    new_mutex.append(m)
            fobj.mutually_exclusive = new_mutex

        # Create visual connections for prerequisites where both nodes exist in canvas
        for orig, fobj, node in pasted_nodes:
            for p in fobj.prerequisites:
                if paste_macro is not None:
                    paste_macro.addCommand(CreateConnectionCommand(self.canvas, p, fobj.id))
                else:
                    try:
                        self.canvas.create_connection(p, fobj.id)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Ensure reciprocity for mutually_exclusive entries on pasted focuses
        for _, fobj, _ in pasted_nodes:
            try:
                self._sync_mutual_exclusive(fobj.id)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # reflow and refresh
        try:
            self.canvas.recompute_lineages()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.canvas.schedule_frame_update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.canvas.refresh_mutex_connectors()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Enforce hidden-focus visibility after connections are created
        try:
            if hasattr(self.canvas, 'apply_hidden_visibility'):
                try:
                    self.canvas.apply_hidden_visibility()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            count = len(pasted_nodes)
        except Exception:
            count = 0
        # push macro to undo stack if present
        if paste_macro is not None and getattr(self, 'undo_stack', None) is not None:
            try:
                self.undo_stack.push(paste_macro)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.statusBar().showMessage(f"Pasted {count} focus(es).")
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def show_multi_add_dialog(self):
        dlg = MultiAddDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        base_id = vals['base_id'] or 'focus'
        base_name = vals['base_name'] or 'New Focus'
        count = vals['count']
        direction = vals['direction']
        sx = vals['start_x']
        sy = vals['start_y']
        gap = vals['gap']
        created = []
        main_uw = getattr(self, 'undo_stack', None)
        multi_macro = None
        if main_uw is not None:
            multi_macro = MacroCommand(description=f"Multi-Add {count} focuses")
        for idx in range(count):
            nid = f"{base_id}{idx+1}"
            # ensure uniqueness — check the prefixed candidate so numbering uses project tag
            suffix = 1
            candidate = nid
            existing_ids = {f.id for f in self.focuses}
            candidate_pref = self._prefix_focus_id(candidate)
            while candidate_pref in existing_ids or candidate_pref in getattr(self.canvas, 'nodes', {}):
                candidate = f"{nid}_{suffix}"
                suffix += 1
                candidate_pref = self._prefix_focus_id(candidate)
            x = sx + (idx * gap if direction == 'Horizontal' else 0)
            y = sy + (idx * gap if direction == 'Vertical' else 0)
            # Prefix with tag and use that as the canonical id
            candidate_pref = self._prefix_focus_id(candidate)
            f = Focus(id=candidate_pref, name=f"{base_name} {idx+1}", x=x, y=y)
            if multi_macro is not None:
                multi_macro.addCommand(AddFocusCommand(self, f, description=f"MultiAdd {f.id}"))
            else:
                try:
                    f.mutually_exclusive = []
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.focuses.append(f)
                self.canvas.add_focus_node(f)
            created.append(f)
        try:
            self.canvas.recompute_lineages()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.canvas.schedule_frame_update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        if multi_macro is not None and getattr(self, 'undo_stack', None) is not None:
            try:
                self.undo_stack.push(multi_macro)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        QMessageBox.information(self, "Multi-Add", f"Created {len(created)} focuses.")

    def colorize_selected_nodes(self):
        """Pick a color and apply to selected nodes (and optionally connections)."""
        # include both focus nodes and event nodes if selected
        items = [n for n in self.canvas.nodes.values() if n.isSelected()]
        items += [n for n in getattr(self.canvas, 'event_nodes', {}).values() if n.isSelected()]
        if not items:
            QMessageBox.information(self, "Colorize", "No nodes selected.")
            return
        color = QColorDialog.getColor(parent=self, title="Pick Node Color")
        if not color.isValid():
            return
        apply_to_connections = QMessageBox.question(self, "Colorize", "Also color connections to/from these nodes?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        def _node_id(n):
            if hasattr(n, 'focus') and getattr(n, 'focus', None) is not None:
                return n.focus.id
            if hasattr(n, 'event') and getattr(n, 'event', None) is not None:
                return n.event.id
            return None

        node_ids = [nid for nid in (_node_id(n) for n in items) if nid is not None]
        uw = getattr(self, 'undo_stack', None)
        if uw is not None:
            try:
                cmd = ColorizeNodesCommand(self, node_ids, color, apply_to_connections)
                uw.push(cmd)
            except Exception:
                # fallback to immediate
                for node in items:
                    nid = _node_id(node)
                    if nid is not None:
                        self.canvas.focus_color_overrides[nid] = color
                if apply_to_connections:
                    for conn in list(self.canvas.connections):
                        if conn.start_node in items or conn.end_node in items:
                            conn.set_color(color)
                try:
                    self.canvas.refresh_connection_colors()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for n in items:
                    n.update()
        else:
            for node in items:
                self.canvas.focus_color_overrides[node.focus.id] = color
            if apply_to_connections:
                for conn in list(self.canvas.connections):
                    if conn.start_node in items or conn.end_node in items:
                        conn.set_color(color)
            try:
                self.canvas.refresh_connection_colors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for n in items:
                n.update()
        QMessageBox.information(self, "Colorize", f"Applied color to {len(items)} node(s).")

    def duplicate_focus(self, original_focus):
        """Create a duplicate of the given focus"""
        new_id = f"{original_focus.id}_copy"
        counter = 1
        while any(f.id == new_id for f in self.focuses):
            new_id = f"{original_focus.id}_copy_{counter}"
            counter += 1
        new_focus = Focus(
            id=new_id,
            name=original_focus.name,
            x=original_focus.x + 1,
            y=original_focus.y,
            cost=original_focus.cost,
            description=original_focus.description,
            prerequisites=original_focus.prerequisites.copy(),
            mutually_exclusive=original_focus.mutually_exclusive.copy(),
            available=original_focus.available,
            bypass=original_focus.bypass,
            completion_reward=original_focus.completion_reward,
            ai_will_do=original_focus.ai_will_do
            ,icon=getattr(original_focus, 'icon', None)
        )
        # Prefer undo for duplications so it can be undone as a single operation
        uw = getattr(self, 'undo_stack', None)
        if uw is not None:
            cmd = AddFocusCommand(self, new_focus, description=f"Duplicate Focus {original_focus.id} -> {new_id}")
            uw.push(cmd)
        else:
            try:
                new_focus._copied = True
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.focuses.append(new_focus)
            self.canvas.add_focus_node(new_focus)
            try:
                self.update_status()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            # Ensure reciprocity for any mutually_exclusive entries copied into the new focus
            try:
                self._sync_mutual_exclusive(new_focus.id)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.canvas.refresh_mutex_connectors()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage(f"Duplicated focus: {new_id}")

    def delete_focus_node(self, node: FocusNode):
        """Delete a focus node and clean up references"""
        uw = getattr(self, 'undo_stack', None)
        if uw is not None:
            cmd = DeleteFocusCommand(self, node)
            uw.push(cmd)
        else:
            focus_id = node.focus.id
            # Remove from focuses list
            self.focuses = [f for f in self.focuses if f.id != focus_id]
            # Remove prerequisites references in other focuses
            for focus in self.focuses:
                if focus_id in focus.prerequisites:
                    focus.prerequisites.remove(focus_id)
                if focus_id in focus.mutually_exclusive:
                    focus.mutually_exclusive.remove(focus_id)
            # Remove from canvas
            self.canvas.remove_node(node)
            self.statusBar().showMessage(f"Deleted focus: {focus_id}")
            try:
                self.update_status()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def toggle_connection_mode(self, checked):
        """Toggle connection creation mode"""
        self.canvas.connection_mode = checked
        if checked:
            self.statusBar().showMessage("Connection mode: Click two focuses to connect them")
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.statusBar().showMessage("Ready")
            self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.canvas.connection_start = None

    def _set_prereq_mode(self, mode: Optional[str]) -> None:
        """Set the canvas prereq link mode: None|'OR'|'AND'"""
        try:
            if mode is None:
                self.canvas.prereq_link_mode = None
                try:
                    self.statusBar().showMessage("Prereq link mode: Normal")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return
            m = str(mode).upper()
            if m not in ('OR', 'AND'):
                self.canvas.prereq_link_mode = None
                try:
                    self.statusBar().showMessage("Prereq link mode: Normal")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return
            self.canvas.prereq_link_mode = m
            try:
                self.statusBar().showMessage(f"Prereq link mode: {m}")
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def toggle_frames(self, checked: bool):
        """Enable/disable frame grouping overlays at runtime."""
        try:
            self.canvas.frames_enabled = bool(checked)
            if not checked:
                self.canvas.clear_frames()
            # mark frames dirty to force rebuild on next update
            self.canvas._frames_dirty = True
            if checked:
                self.canvas.update_frames()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def show_find_notes_dialog(self):
        """List all notes and center the view on the chosen one."""
        try:
            notes = [it for it in getattr(self.canvas, '_notes_items', []) if isinstance(it, NoteNode)]
            if not notes:
                QMessageBox.information(self, "Find Notes", "There are no notes on the canvas.")
                return
            dlg = FindNotesDialog(notes, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                target = dlg.selected_note()
                if target is not None:
                    try:
                        self.view.centerOn(target)
                        target.setSelected(True)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # -------------------------
    # Generation: UI + helpers
    # -------------------------
    def show_generate_dialog(self):
        """Show a simplified dialog to collect generation parameters and produce a project."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Generate Project")
        outer_layout = QVBoxLayout()
        try:
            dialog.resize(900, 700)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Use a toolbox: Basic vs Advanced
        toolbox = QToolBox()
        # Basic page
        basic_page = QWidget(); basic_form = QFormLayout(basic_page)
        tree_id_edit = QLineEdit(self.tree_id); tree_id_edit.setToolTip("Tree id used in exported HOI4 file and focus ids prefix")
        seed_edit = QLineEdit(""); seed_edit.setPlaceholderText("random if empty")
        seed_edit.setToolTip("Leave empty for random seed. Same seed -> reproducible structure and variation")
        # Generation mode
        mode_combo = QComboBox(); mode_combo.addItems(["Depth-based (levels)", "Growth-based (node count)"])
        mode_combo.setToolTip("Pick how the tree is grown: fixed depth/branching or stochastic growth by target node count")
        # Depth controls
        root_count_spin = QSpinBox(); root_count_spin.setRange(1, 8); root_count_spin.setValue(2)
        root_count_spin.setToolTip("How many independent roots (top-level focuses)")
        max_depth_spin = QSpinBox(); max_depth_spin.setRange(1, 12); max_depth_spin.setValue(5)
        max_depth_spin.setToolTip("Max vertical depth below roots (levels)")
        min_branch_spin = QSpinBox(); min_branch_spin.setRange(1, 6); min_branch_spin.setValue(1)
        max_branch_spin = QSpinBox(); max_branch_spin.setRange(1, 6); max_branch_spin.setValue(2)
        min_branch_spin.setToolTip("Minimum children per node (inclusive)")
        max_branch_spin.setToolTip("Maximum children per node (inclusive)")
        # Growth controls
        node_count_spin = QSpinBox(); node_count_spin.setRange(10, 2000); node_count_spin.setValue(120)
        node_count_spin.setToolTip("Approximate number of focuses to generate")
        branch_density_spin = QDoubleSpinBox(); branch_density_spin.setRange(0.0, 15.0); branch_density_spin.setSingleStep(0.1); branch_density_spin.setValue(5.0)
        branch_density_spin.setToolTip("Higher -> more branching per step (stochastic)")
        max_children_spin = QSpinBox(); max_children_spin.setRange(0, 10); max_children_spin.setValue(0)
        max_children_spin.setToolTip("Limit children per node (0 = no extra cap beyond branching range)")
        enforce_depth_cb = QCheckBox("Enforce max depth in growth mode")
        enforce_depth_cb.setChecked(True)
        # Layout randomness slider
        layout_rand_slider = QSlider(Qt.Orientation.Horizontal); layout_rand_slider.setRange(0, 100); layout_rand_slider.setValue(35)
        layout_rand_label = QLabel("35%")
        layout_rand_slider.setToolTip("Adds jitter and slight re-ordering to produce varied layouts while keeping prerequisites")
        def _on_lr(v):
            layout_rand_label.setText(f"{v}%")
        layout_rand_slider.valueChanged.connect(_on_lr)
        lr_box = QHBoxLayout(); lr_box.addWidget(layout_rand_slider); lr_box.addWidget(layout_rand_label)

        # Layout style controls
        layout_style_combo = QComboBox(); layout_style_combo.addItems(["organic", "tidy", "clustered", "radial", "zigzag", "wave"])
        layout_style_combo.setCurrentText("organic")
        layout_style_combo.setToolTip("Default style when not using per-root styles or weighted mix")
        per_root_styles_edit = QLineEdit(""); per_root_styles_edit.setPlaceholderText("Per-root styles CSV, e.g. organic,radial,wave")
        per_root_styles_edit.setToolTip("Comma-separated styles for each root; cycles if fewer than roots")
        mix_edit = QLineEdit(""); mix_edit.setPlaceholderText("Weighted mix, e.g. organic:2,radial:1,clustered:1")
        mix_edit.setToolTip("Weighted random selection per root; format style:weight,style:weight")

        # Naming options
        use_theme_cb = QCheckBox("Use theme names (JSON)"); use_theme_cb.setChecked(True)
        use_lib_cb = QCheckBox("Use library names as fallback"); use_lib_cb.setChecked(True)
        # Suggest default theme if available
        default_theme_path = None
        try:
            default_theme_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'focusTool', 'madmax_theme.json'))
            if not os.path.isfile(default_theme_path):
                default_theme_path = None
        except Exception:
            default_theme_path = None
        theme_prefill = self.theme_path or (default_theme_path or "")
        theme_path_edit = QLineEdit(theme_prefill); theme_path_edit.setPlaceholderText("Select theme JSON (categories -> lists)")
        browse_btn = QPushButton("Browse…")
        def _browse():
            fn, _ = QFileDialog.getOpenFileName(self, "Select Theme JSON", "", "JSON Files (*.json)")
            if fn:
                theme_path_edit.setText(fn)
        browse_btn.clicked.connect(_browse)
        theme_row = QHBoxLayout(); theme_row.addWidget(theme_path_edit); theme_row.addWidget(browse_btn)

        # Basic form rows
        basic_form.addRow("Tree ID:", tree_id_edit)
        basic_form.addRow("Seed:", seed_edit)
        basic_form.addRow("Generation mode:", mode_combo)
        basic_form.addRow(QLabel("Layout randomness:"), QWidget())
        basic_form.addRow(lr_box)
        basic_form.addRow("Default layout style:", layout_style_combo)
        basic_form.addRow("Per-root styles:", per_root_styles_edit)
        basic_form.addRow("Weighted style mix:", mix_edit)
        basic_form.addRow(use_theme_cb)
        basic_form.addRow("Theme file:", QWidget())
        basic_form.addRow(theme_row)
        basic_form.addRow(use_lib_cb)

        # Depth controls group
        depth_group = QGroupBox("Depth-based options"); depth_layout = QFormLayout(depth_group)
        depth_layout.addRow("Root count:", root_count_spin)
        depth_layout.addRow("Max depth:", max_depth_spin)
        depth_layout.addRow("Min branch:", min_branch_spin)
        depth_layout.addRow("Max branch:", max_branch_spin)

        # Growth controls group
        growth_group = QGroupBox("Growth-based options"); growth_layout = QFormLayout(growth_group)
        growth_layout.addRow("Target node count:", node_count_spin)
        growth_layout.addRow("Branch density:", branch_density_spin)
        growth_layout.addRow("Max children per node:", max_children_spin)
        growth_layout.addRow(enforce_depth_cb)

        # toggle visibility based on mode
        def _update_mode():
            is_growth = (mode_combo.currentIndex() == 1)
            depth_group.setVisible(not is_growth)
            growth_group.setVisible(is_growth)
        mode_combo.currentIndexChanged.connect(lambda _: _update_mode())
        _update_mode()

        depth_wrap = QWidget(); depth_v = QVBoxLayout(depth_wrap); depth_v.addWidget(depth_group); depth_v.addWidget(growth_group)
        basic_form.addRow(depth_wrap)

        toolbox.addItem(basic_page, "Basic")

        # Advanced page
        adv_page = QWidget(); adv_form = QFormLayout(adv_page)
        mutex_cb = QCheckBox("Mutually exclusive root branches"); mutex_cb.setChecked(False)
        # Sibling-level mutex controls
        mutex_siblings_cb = QCheckBox("Make sibling focuses mutually exclusive")
        mutex_siblings_cb.setChecked(False)
        mutex_mode_combo = QComboBox(); mutex_mode_combo.addItems(["all", "ring", "pairs"])
        mutex_mode_combo.setCurrentText("all")
        mutex_prob = QDoubleSpinBox(); mutex_prob.setRange(0.0, 1.0); mutex_prob.setSingleStep(0.05); mutex_prob.setValue(1.0)
        adv_form.addRow(mutex_siblings_cb)
        adv_form.addRow("Sibling mutex mode:", mutex_mode_combo)
        adv_form.addRow("Sibling mutex probability:", mutex_prob)
        run_in_bg_cb = QCheckBox("Run in background"); run_in_bg_cb.setChecked(True)
        networks_spin = QSpinBox(); networks_spin.setRange(1, 20); networks_spin.setValue(1)
        network_size_spin = QSpinBox(); network_size_spin.setRange(1, 300); network_size_spin.setValue(100)
        adv_form.addRow(mutex_cb)
        adv_form.addRow("Number of networks:", networks_spin)
        adv_form.addRow("Network size limit:", network_size_spin)
        adv_form.addRow(run_in_bg_cb)
        toolbox.addItem(adv_page, "Advanced")

        outer_layout.addWidget(toolbox)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        outer_layout.addWidget(buttons)
        dialog.setLayout(outer_layout)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # collect params
        params: Dict[str, Any] = {
            'tree_id': tree_id_edit.text().strip() or self.tree_id,
            'use_library_names': use_lib_cb.isChecked(),
            'use_theme_names': use_theme_cb.isChecked(),
            'layout_randomness': layout_rand_slider.value() / 100.0,
            'layout_style': layout_style_combo.currentText(),
            'add_mutex_between_branches': mutex_cb.isChecked(),
            'seed': int(seed_edit.text()) if seed_edit.text().strip().isdigit() else None,
            'networks': int(networks_spin.value()),
            'network_size': int(network_size_spin.value())
        }

    # Mode-specific
        if mode_combo.currentIndex() == 0:
            params['mode'] = 'depth'
            params['root_count'] = root_count_spin.value()
            params['max_depth'] = max_depth_spin.value()
            params['branching'] = (min_branch_spin.value(), max_branch_spin.value())
            params['node_count'] = None
            params['branch_density'] = 5.0
        else:
            params['mode'] = 'growth'
            params['root_count'] = max(1, root_count_spin.value())
            params['max_depth'] = max_depth_spin.value()
            params['branching'] = (min_branch_spin.value(), max_branch_spin.value())
            params['node_count'] = node_count_spin.value()
            params['branch_density'] = float(branch_density_spin.value())
            params['max_children_per_node'] = int(max_children_spin.value()) or None
            params['enforce_depth_cap'] = bool(enforce_depth_cb.isChecked())

        # Theme loading (optional)
        theme_dict: Optional[Dict[str, List[str]]] = None
        if use_theme_cb.isChecked():
            path = (theme_path_edit.text() or '').strip()
            if path:
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        theme_dict = {k: v for k, v in data.items() if isinstance(v, list)}
                        # persist on window
                        self.theme_data = data
                        self.theme_path = path
                except Exception as e:
                    QMessageBox.warning(self, "Theme", f"Failed to load theme file: {e}\nProceeding without theme.")
        params['theme'] = theme_dict
        # sibling mutex params
        params['mutex_siblings'] = bool(mutex_siblings_cb.isChecked())
        params['mutex_sibling_mode'] = mutex_mode_combo.currentText()
        params['mutex_sibling_probability'] = float(mutex_prob.value())

        # Parse per-root styles CSV
        txt = (per_root_styles_edit.text() or '').strip()
        if txt:
            styles = [s.strip() for s in txt.split(',') if s.strip()]
            if styles:
                params['layout_styles'] = styles
        # Parse weighted mix
        mix_txt = (mix_edit.text() or '').strip()
        if mix_txt:
            mix_list = []
            for item in mix_txt.split(','):
                if not item.strip():
                    continue
                if ':' in item:
                    st, w = item.split(':', 1)
                    try:
                        mix_list.append((st.strip(), float(w)))
                    except Exception:
                        mix_list.append((st.strip(), 1.0))
                else:
                    mix_list.append((item.strip(), 1.0))
            if mix_list:
                params['layout_mix'] = mix_list

        # Capture GUI state copies for background thread
        try:
            params['_library'] = dict(self.library) if isinstance(self.library, dict) else self.library
        except Exception:
            params['_library'] = {}
        params['_country_tag'] = str(self.country_tag)

        # Use a background Python thread for generation and marshal results back to the Qt thread
        progress = QProgressDialog("Generating project...", "", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setWindowTitle("Generating")
        # Make dialog indeterminate and non-cancellable (generator is not interruptible)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)

        def on_finished(project):
            progress.close()
            # release refs
            try:
                self._gen_thread = None
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Inform user how many focuses were generated
            try:
                count = len(project.get('focuses', [])) if isinstance(project, dict) else 0
            except Exception:
                count = 0
            QMessageBox.information(self, "Generation Complete", f"Generated {count} focuses.")
            if count > 0:
                try:
                    self.load_project_from_dict(project)
                except Exception as e:
                    show_error(self, "Load Error", "Failed to load generated project.", exc=e)
                # Ensure window and view refresh so nodes are visible when generation ran in background
                try:
                    self.raise_()
                    try:
                        self.activateWindow()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # repaint view and schedule a fit in the next event loop
                    try:
                        self.view.viewport().update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    QTimer.singleShot(0, lambda: (self.update_status(), self.fit_view()))
                except Exception as e:
                    logger.exception("[generator] post-load refresh failed")
            else:
                # Use consistent projects folder for generated projects
                default_dir = getattr(self, 'projects_home_path', None)
                if not default_dir:
                    abd = getattr(self, 'app_base_dir', None)
                    if abd:
                        default_dir = os.path.join(abd, 'projects')
                if not default_dir:
                    default_dir = os.getcwd()

                # Sanitize tree_id for filename
                try:
                    import re
                    raw_tree_id = str(project.get('tree_id', '') or 'generated')
                    safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', raw_tree_id).strip('_') or 'generated'
                except Exception:
                    safe_tree_id = 'generated'

                default_name = f"{safe_tree_id}.json"
                default_path = os.path.join(default_dir, default_name)
                filename, _ = QFileDialog.getSaveFileName(self, "Save Generated Project", default_path, "JSON Files (*.json)")
                if filename:
                    try:
                        with open(filename, 'w', encoding='utf-8') as f:
                            json.dump(project, f, indent=2, ensure_ascii=False)
                        QMessageBox.information(self, "Saved", f"Generated project saved to {obfuscate_path(filename)}")
                    except Exception as e:
                        show_error(self, "Error", "Failed to save.", exc=e)

        def on_error(msg):
            progress.close()
            try:
                self._gen_thread = None
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            show_error(self, "Generation Error", obfuscate_text(msg))

        def _run_generation():
            try:
                logger.debug("[generator] starting generation with params: %s", params)
                proj = self.generate_project_dict(params)
                logger.debug("[generator] finished generation; focuses=%s", len(proj.get('focuses', [])) if isinstance(proj, dict) else 'N/A')
                # emit signal (thread-safe) to schedule on main thread
                try:
                    self.generation_finished.emit(proj)
                except Exception:
                    # fallback to QTimer if signals are not available
                    QTimer.singleShot(0, lambda p=proj: on_finished(p))
            except Exception as e:
                logger.exception("[generator] exception during generation")
                try:
                    self.generation_error.emit(str(e))
                except Exception:
                    QTimer.singleShot(0, lambda m=str(e): on_error(m))
        # If user disabled background execution for debugging, run sync
        if not run_in_bg_cb.isChecked():
            self.statusBar().showMessage("Generating project (synchronous)...")
            try:
                proj = self.generate_project_dict(params)
                on_finished(proj)
            except Exception as e:
                on_error(str(e))
            return

        self.statusBar().showMessage("Generating project...")
        # connect signals to handlers so emitted results are handled on GUI thread
        try:
            self.generation_finished.connect(on_finished)
            self.generation_error.connect(on_error)
        except Exception:
            # ignore: will fallback to QTimer.post
            pass
        t = threading.Thread(target=_run_generation, daemon=True)
        self._gen_thread = t
        t.start()
        progress.show()

    def generate_project_dict(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Use FocusTreeGenerator to produce a project dict compatible with save/load."""
        logger.debug("[generator] generate_project_dict called with: %s", params)
        try:
            from _focusGenerator import FocusTreeGenerator
        except Exception as e:
            # Do not call any GUI functions here; return an empty project and log
            logger.exception("[generator] import error in generator module")
            return {'version': params.get('_app_version', '1.0.9'), 'tree_id': params.get('tree_id','generated'), 'country_tag': params.get('_country_tag','TAG'), 'focuses': [], 'library': params.get('_library', {})}
        try:
            # Use captured snapshots from params to avoid accessing GUI state from background thread
            lib = params.get('_library', {})
            country = params.get('_country_tag', 'TAG')
            gen = FocusTreeGenerator(library=lib, country_tag=country, id_prefix=params.get('tree_id','gen'), theme=params.get('theme'))
            # Support multiple Networks by invoking generator multiple times and tagging network_id
            networks = int(params.get('networks', 1))
            network_size = int(params.get('network_size', 100))
            focuses = []
            base_seed = params.get('seed', None)
            for net in range(max(1, networks)):
                try:
                    seed_for = None if base_seed is None else int(base_seed) + net
                except Exception:
                    seed_for = base_seed
                part = gen.generate(
                    tree_id=f"{params.get('tree_id','generated')}_net{net}",
                    root_count=params.get('root_count',1),
                    max_depth=params.get('max_depth',4),
                    branching=params.get('branching',(1,2)),
                    use_library_names=params.get('use_library_names',True),
                    use_theme_names=params.get('use_theme_names', True),
                    add_mutex_between_branches=params.get('add_mutex_between_branches',False),
                    seed=seed_for,
                    node_count=min(params.get('node_count') or network_size, network_size) if networks > 1 else params.get('node_count', None),
                    branch_density=params.get('branch_density', 5.0),
                    max_children_per_node=params.get('max_children_per_node', None),
                    enforce_depth_cap=bool(params.get('enforce_depth_cap', True)),
                    mutex_siblings=bool(params.get('mutex_siblings', False)),
                    mutex_sibling_mode=params.get('mutex_sibling_mode', 'all'),
                    mutex_sibling_probability=float(params.get('mutex_sibling_probability', 1.0)),
                    layout_randomness=float(params.get('layout_randomness', 0.3)),
                    theme=params.get('theme')
                )
                # tag network id on generated focuses; if more than network_size, mark with -1 (SuperNet)
                for i, f in enumerate(part):
                    try:
                        if i < network_size:
                            f.network_id = net
                        else:
                            f.network_id = -1
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                focuses.extend(part)
        except Exception as e:
            logger.exception("[generator] generation exception")
            focuses = []
        project = {
            'version': params.get('_app_version', '1.0.9'),
            'tree_id': params.get('tree_id','generated'),
            'country_tag': self.country_tag,
            'focuses': [],
            'library': self.library
        }
        for f in focuses:
            try:
                project['focuses'].append({
                    'id': f.id,
                    'name': f.name,
                    'x': f.x,
                    'y': f.y,
                    'cost': f.cost,
                    'description': getattr(f, 'description', ''),
                    'prerequisites': getattr(f, 'prerequisites', []).copy(),
                    'mutually_exclusive': getattr(f, 'mutually_exclusive', []).copy(),
                    'available': getattr(f, 'available', ''),
                    'bypass': getattr(f, 'bypass', ''),
                    'completion_reward': getattr(f, 'completion_reward', ''),
                    'ai_will_do': getattr(f, 'ai_will_do', 1),
                    'network_id': getattr(f, 'network_id', None),
                    'icon': getattr(f, 'icon', None)
                })
            except Exception:
                logger.exception("[generator] skipping focus due to error")
        logger.info("[generator] project built with %d focuses", len(project['focuses']))
        return project

    def load_project_from_dict(self, project: Dict[str, Any]):
        """Load a project provided as a dict into the current GUI (clears existing)."""
        logger.debug("[loader] load_project_from_dict called; focuses=%s", len(project.get('focuses', [])) if isinstance(project, dict) else 'N/A')
        # Prevent the canvas auto-layout/reflow routines from running while we
        # construct nodes and connections. Some projects were being re-positioned
        # on load because the loader recenters/scales and then triggers
        # reflow/auto-layout; suspend layout and temporarily disable
        # auto_layout to preserve persisted positions on disk.
        prev_suspend = getattr(self.canvas, '_suspend_layout', False)
        prev_auto_layout = getattr(self.canvas, 'auto_layout_enabled', False)
        try:
            setattr(self.canvas, '_suspend_layout', True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            # Temporarily disable auto layout so the reflow block at the end
            # of this function does not run during load.
            self.canvas.auto_layout_enabled = False
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Clear current
        self.canvas.clear()
        self.canvas.draw_grid()
        self.focuses.clear()
        self.canvas.nodes.clear()
        self.canvas.connections.clear()
        try:
            if hasattr(self.canvas, '_spatial_index'):
                self.canvas._spatial_index.clear()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            if hasattr(self.canvas, '_visible_nodes_cache'):
                self.canvas._visible_nodes_cache.clear()
        except Exception:
            try:
                self.canvas._visible_nodes_cache = weakref.WeakSet()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # reset events
        try:
            self.events = []
            if hasattr(self.canvas, 'event_nodes'):
                self.canvas.event_nodes.clear()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # If project contains persisted settings, apply them early
        try:
            proj_settings = project.get('settings') or {}
            if isinstance(proj_settings, dict):
                # read project-level preference for app-settings precedence if present
                try:
                    prefs = proj_settings.get('preferences') or {}
                    self.prefer_app_settings = bool(prefs.get('prefer_app_settings', getattr(self, 'prefer_app_settings', False)))
                except Exception:
                    # keep existing value if any
                    self.prefer_app_settings = bool(getattr(self, 'prefer_app_settings', False))

                # project canvas may be absent; prefer_app_settings should still allow
                # overlaying an app-wide cached canvas when requested
                canv = proj_settings.get('canvas') if isinstance(proj_settings, dict) else None
                applied_any = False
                if isinstance(canv, dict) and canv:
                    # Apply project canvas settings first
                    try:
                        self.canvas.apply_settings(canv)
                        applied_any = True
                    except Exception:
                        applied_any = applied_any or False

                # If user prefers app-wide settings, layer them after project (or apply them
                # directly when the project had no canvas settings)
                try:
                    if bool(getattr(self, 'prefer_app_settings', False)) and isinstance(getattr(self, '_app_canvas_settings_cache', None), dict):
                        try:
                            self.canvas.apply_settings(self._app_canvas_settings_cache)
                            applied_any = True
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # CRITICAL FIX: Ensure connection LOD threshold and dynamic scaling are set correctly
                # Even if old settings were applied, we want to use the new defaults
                # unless explicitly set to something different in the project/app settings
                # This prevents old projects from showing straight lines instead of curves
                # and ensures dynamic scaling is enabled for better large-tree visibility
                try:
                    # Only override if the project settings didn't explicitly set a different value
                    if not (isinstance(canv, dict) and 'connection_lod_threshold' in canv):
                        # Project didn't have a custom LOD threshold, use new default
                        if not (isinstance(self._app_canvas_settings_cache, dict) and 'connection_lod_threshold' in self._app_canvas_settings_cache):
                            # App settings also don't have custom LOD threshold, force new default
                            self.canvas.connection_lod_threshold = 0.0

                    # Similarly ensure dynamic scaling is enabled for old projects
                    if not (isinstance(canv, dict) and 'enable_dynamic_title_icon_scaling' in canv):
                        if not (isinstance(self._app_canvas_settings_cache, dict) and 'enable_dynamic_title_icon_scaling' in self._app_canvas_settings_cache):
                            self.canvas.enable_dynamic_title_icon_scaling = True
                            self.canvas.title_icon_scale_zoom_threshold = 0.3
                            self.canvas.title_icon_scale_max_multiplier = 2.5
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Apply project-level keybindings if present and the user does NOT prefer app-level settings
                try:
                    project_kb = (proj_settings.get('keybindings') if isinstance(proj_settings, dict) else None) or None
                    # If the project explicitly requests project-level bindings (or user doesn't prefer app settings), apply them
                    if isinstance(project_kb, dict) and project_kb:
                        try:
                            if not bool(getattr(self, 'prefer_app_settings', False)) and getattr(self, 'keybinds', None) is not None:
                                self.keybinds.apply_mapping(project_kb)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # sync UI toggle states after applying any canvas settings
                try:
                    if applied_any:
                        self.frames_action.setChecked(bool(self.canvas.frames_enabled))
                        self.lineage_color_action.setChecked(bool(self.canvas.color_lines_by_lineage))
                        self.grid_action.setChecked(bool(getattr(self.canvas, '_grid_visible', True)))
                        if hasattr(self, 'icon_view_action'):
                            self.icon_view_action.setChecked(bool(getattr(self.canvas, 'icon_view_mode', False)))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Load metadata
        self.tree_id = project.get('tree_id', 'custom_focus_tree')
        self.tree_id_edit.setText(self.tree_id)
        self.country_tag = project.get('country_tag', 'TAG')
        self.country_edit.setText(self.country_tag)
        if 'library' in project and isinstance(project['library'], dict):
            self.library = project['library']
            self.refresh_library_list()
        # Restore icon library if the project bundled one
        if 'icon_library' in project and isinstance(project['icon_library'], dict):
            try:
                self.icon_library = project['icon_library']
                # ensure UI reflects new icon library
                try:
                    self.refresh_library_list()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # If the project includes persisted state/province data, load it into the State Viewport
        try:
            if hasattr(self, 'state_viewport_dock') and getattr(self, 'state_viewport_dock', None) is not None:
                # Prefer a per-project sidecar states file if present (recorded in settings)
                try:
                    settings = project.get('settings', {}) or {}
                    sv = settings.get('state_viewport', {}) or {}
                    last_map_name = sv.get('last_map')
                except Exception:
                    last_map_name = None

                loaded = False
                if last_map_name and getattr(self, 'current_project_path', None):
                    try:
                        proj_dir = os.path.dirname(getattr(self, 'current_project_path')) or '.'
                        sidecar_path = os.path.join(proj_dir, str(last_map_name))
                        if os.path.isfile(sidecar_path):
                            try:
                                with open(sidecar_path, 'r', encoding='utf-8') as sf:
                                    data = json.load(sf)
                                self.state_viewport_dock.load_states_from_dict(data, source_path=sidecar_path, quiet=True)
                                loaded = True
                            except Exception:
                                loaded = False
                    except Exception:
                        loaded = False

                if not loaded:
                    has_states = isinstance(project.get('states', None), dict)
                    has_provinces = isinstance(project.get('provinces', None), (dict, list))
                    if has_states or has_provinces:
                        payload = {
                            'states': project.get('states', {}) or {},
                            'provinces': project.get('provinces', {}) or {}
                        }
                        try:
                            self.state_viewport_dock.load_states_from_dict(payload, source_path=getattr(self, 'current_project_path', None), quiet=True)
                        except Exception:
                            # Try a minimal fallback without provinces if type mismatch
                            try:
                                self.state_viewport_dock.load_states_from_dict({'states': payload.get('states', {})}, source_path=getattr(self, 'current_project_path', None), quiet=True)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Apply project note defaults and restore notes/connections if present
        try:
            nd = project.get('note_defaults') or {}
            if isinstance(nd, dict):
                self.canvas.note_defaults.update(nd)
            # clear and recreate notes
            self.canvas.clear_notes()
            id_to_note: Dict[str, NoteNode] = {}
            # build lookup for focuses by id for note→focus links
            focus_id_to_node: Dict[str, FocusNode] = {}
            for nd in (project.get('notes') or []):
                try:
                    if isinstance(nd, dict):
                        it = NoteNode.from_dict(nd)
                        self.canvas.addItem(it)
                        it.set_visible(self.canvas.notes_enabled)
                        self.canvas._notes_items.append(it)
                        if getattr(it, 'note_id', None):
                            id_to_note[it.note_id] = it
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for pair in (project.get('note_connections') or []):
                try:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2:
                        a = id_to_note.get(str(pair[0])); b = id_to_note.get(str(pair[1]))
                        if a and b:
                            self.canvas.add_note_connection(a, b)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # defer creating note→focus links until focuses are built; store pairs temporarily
            self._deferred_note_focus_links = list(project.get('note_focus_links', [])) if isinstance(project.get('note_focus_links', []), list) else []
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Create focuses
        # Preserve saved positions by default. If desired, a future setting
        # 'normalize_on_load' can be set True to fit/scale trees to the scene.
        import math
        focuses_data = list(project.get('focuses', []))
        # Sanitize coordinate types but preserve numeric precision.
        # We intentionally keep x/y as floats so we can apply tiny visual
        # offsets for duplicated positions without mutating the original
        # integer grid coordinates when they are unique (1:1 preservation).
        sanitized = []
        for d in focuses_data:
            if not isinstance(d, dict):
                continue
            dd = dict(d)
            try:
                if 'x' in dd:
                    # keep as float when possible (e.g., '1' -> 1.0)
                    dd['x'] = float(dd['x'])
                else:
                    dd['x'] = 0.0
            except Exception:
                try:
                    dd['x'] = float(0)
                except Exception:
                    dd['x'] = 0.0
            try:
                if 'y' in dd:
                    dd['y'] = float(dd['y'])
                else:
                    dd['y'] = 0.0
            except Exception:
                try:
                    dd['y'] = float(0)
                except Exception:
                    dd['y'] = 0.0
            # Ensure optional numeric types are sane
            try:
                if 'ai_will_do' in dd:
                    dd['ai_will_do'] = int(dd.get('ai_will_do', 1))
            except Exception:
                dd['ai_will_do'] = 1
            # network id may be None or int; coerce when provided
            try:
                if dd.get('network_id') is not None:
                    dd['network_id'] = int(dd['network_id'])
            except Exception:
                dd['network_id'] = None
            sanitized.append(dd)
        focuses_data = sanitized
        # Ensure no two focuses occupy the same integer grid cell while
        # preserving original positions when they are unique. Many focus
        # trees use identical coordinates (or coordinates that round to the
        # same grid cell). We treat the integer grid cell (rounded grid
        # coordinates) as the visual cell and deterministically relocate any
        # duplicates to the nearest unused integer grid cell. The first
        # focus that maps to a given cell keeps its exact supplied coords.
        try:
            # Preserve the original float coords for all focuses. Only when
            # there are exact duplicates (same float x,y) do we relocate
            # entries. When relocating, prefer placing a duplicate near its
            # parent focus (if the parent has a distinct position) so the
            # relationship graph is preserved visually. Otherwise, place
            # duplicates deterministically around the original key.
            from collections import defaultdict, deque

            # map id->index and build parent relationships
            id_to_index = {}
            for idx, d in enumerate(focuses_data):
                try:
                    fid = str(d.get('id'))
                except Exception:
                    fid = str(idx)
                id_to_index[fid] = idx

            parents = defaultdict(list)
            children = defaultdict(list)
            for idx, d in enumerate(focuses_data):
                for p in (d.get('prerequisites') or []):
                    try:
                        pid = str(p)
                        if pid in id_to_index:
                            parents[idx].append(id_to_index[pid])
                            children[id_to_index[pid]].append(idx)
                    except Exception:
                        continue

            # Build map of exact float positions -> list of indices
            pos_map = defaultdict(list)
            for idx, d in enumerate(focuses_data):
                try:
                    key = (float(d.get('x', 0.0)), float(d.get('y', 0.0)))
                except Exception:
                    key = (0.0, 0.0)
                pos_map[key].append(idx)

            # helper to find nearest free integer cell around a target
            def nearest_free_around(target_cell, occupied_set, max_radius=50):
                sx, sy = target_cell
                if target_cell not in occupied_set:
                    return target_cell
                for r in range(1, max_radius + 1):
                    candidates = []
                    for dx in range(-r, r + 1):
                        dy1 = r - abs(dx)
                        for dy in (-dy1, dy1):
                            cx = sx + dx; cy = sy + dy
                            candidates.append((abs(dx) + abs(dy), dx, dy, cx, cy))
                    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
                    for _, _, _, cx, cy in candidates:
                        if (cx, cy) not in occupied_set:
                            return (cx, cy)
                # fallback linear scan
                for cx in range(sx - max_radius, sx + max_radius + 1):
                    for cy in range(sy - max_radius, sy + max_radius + 1):
                        if (cx, cy) not in occupied_set:
                            return (cx, cy)
                return target_cell

            occupied = set()
            # pre-fill occupied with cells claimed by exactly one index so we
            # don't accidentally move those
            for key, idxs in pos_map.items():
                if len(idxs) == 1:
                    gx = int(round(key[0])); gy = int(round(key[1]))
                    occupied.add((gx, gy))

            # process groups with duplicates
            for key, idxs in pos_map.items():
                if len(idxs) <= 1:
                    continue
                # For determinism, sort indices so nodes with parents are
                # placed near their parent's cell first.
                def parent_score(i):
                    ps = parents.get(i, [])
                    return 0 if ps else 1

                ordered = sorted(idxs, key=lambda i: (parent_score(i), i))
                # deterministic micro-offset generator to avoid visual stacking
                def micro_offsets_for_identifier(identifier):
                    # returns (fx, fy) fractional offsets in range (-0.25, 0.25)
                    try:
                        h = abs(hash(identifier)) & 0xffffffff
                    except Exception:
                        h = int(identifier) if isinstance(identifier, int) else 0
                    # two independent pseudo-random small fractions
                    fx = ((h & 0xffff) / 0xffff) - 0.5
                    fy = (((h >> 16) & 0xffff) / 0xffff) - 0.5
                    # scale down to keep rounding stable (<0.4 ensures rounding stays the same)
                    return (fx * 0.35, fy * 0.35)

                # ensure the first item in ordered attempts to keep its exact
                # float coords (if its rounded cell isn't already occupied by
                # a unique claimant). If occupied, find nearest free around
                # its own rounded cell. For moved items we place them at the
                # chosen integer cell plus a deterministic micro-offset so
                # they remain distinct but still round to that integer cell.
                for j, idx in enumerate(ordered):
                    fx = float(focuses_data[idx].get('x', 0.0))
                    fy = float(focuses_data[idx].get('y', 0.0))
                    rounded = (int(round(fx)), int(round(fy)))
                    fid = focuses_data[idx].get('id', idx)
                    # first claimant: keep float if rounded cell is free
                    if j == 0:
                        if rounded in occupied:
                            new_cell = nearest_free_around(rounded, occupied)
                            occupied.add(new_cell)
                            ox, oy = micro_offsets_for_identifier(str(fid))
                            focuses_data[idx]['x'] = float(new_cell[0]) + ox
                            focuses_data[idx]['y'] = float(new_cell[1]) + oy
                        else:
                            occupied.add(rounded)
                            # keep original float coords (do not overwrite)
                    else:
                        # Prefer to place near a parent's rounded cell if any
                        pidxs = parents.get(idx, [])
                        placed = False
                        for pidx in pidxs:
                            try:
                                px = float(focuses_data[pidx].get('x', 0.0))
                                py = float(focuses_data[pidx].get('y', 0.0))
                                pcell = (int(round(px)), int(round(py)))
                                new_cell = nearest_free_around(pcell, occupied)
                                if new_cell not in occupied:
                                    occupied.add(new_cell)
                                    ox, oy = micro_offsets_for_identifier(str(fid) + str(pidx))
                                    focuses_data[idx]['x'] = float(new_cell[0]) + ox
                                    focuses_data[idx]['y'] = float(new_cell[1]) + oy
                                    placed = True
                                    break
                            except Exception:
                                continue
                        if not placed:
                            # fall back to nearest around the original rounded cell
                            new_cell = nearest_free_around(rounded, occupied)
                            occupied.add(new_cell)
                            ox, oy = micro_offsets_for_identifier(str(fid))
                            focuses_data[idx]['x'] = float(new_cell[0]) + ox
                            focuses_data[idx]['y'] = float(new_cell[1]) + oy
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        normalize = bool(getattr(self, 'normalize_on_load', False))
        if normalize and focuses_data:
            # compute generated bounds and fit within scene bounds if requested
            xs = [d.get('x', 0) for d in focuses_data]
            ys = [d.get('y', 0) for d in focuses_data]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            gen_span_x = max_x - min_x if max_x != min_x else 1
            gen_span_y = max_y - min_y if max_y != min_y else 1
            scene = self.canvas.sceneRect()
            grid_unit = GRID_UNIT
            allowed_min_x = int(math.ceil(scene.left() / grid_unit))
            allowed_max_x = int(math.floor(scene.right() / grid_unit))
            allowed_min_y = int(math.ceil(scene.top() / grid_unit))
            allowed_max_y = int(math.floor(scene.bottom() / grid_unit))
            allowed_span_x = max(1, allowed_max_x - allowed_min_x)
            allowed_span_y = max(1, allowed_max_y - allowed_min_y)
            scale_x = min(1.0, allowed_span_x / float(gen_span_x))
            scale_y = min(1.0, allowed_span_y / float(gen_span_y))
            scale = min(scale_x, scale_y)
            if scale < 1.0:
                logger.debug("[loader] scaling generated layout by %.3f to fit canvas bounds", scale)
            target_center_x = (allowed_min_x + allowed_max_x) / 2.0
            target_center_y = (allowed_min_y + allowed_max_y) / 2.0
            gen_center_x = (min_x + max_x) / 2.0
            gen_center_y = (min_y + max_y) / 2.0
            for d in focuses_data:
                ox = d.get('x', 0)
                oy = d.get('y', 0)
                nx = target_center_x + (ox - gen_center_x) * scale
                ny = target_center_y + (oy - gen_center_y) * scale
                d['x'] = int(round(nx))
                d['y'] = int(round(ny))
            for d in focuses_data:
                d['x'] = max(allowed_min_x, min(allowed_max_x, int(d['x'])))
                d['y'] = max(allowed_min_y, min(allowed_max_y, int(d['y'])))
            occupied = set()
            for d in focuses_data:
                pos = (d['x'], d['y'])
                while pos in occupied:
                    d['x'] += 1
                    if d['x'] > allowed_max_x:
                        d['x'] = allowed_min_x
                        d['y'] += 1
                        if d['y'] > allowed_max_y:
                            d['y'] = allowed_min_y
                    pos = (d['x'], d['y'])
                occupied.add(pos)

        # Compute palette for networks present in the generated data so frames/colors are ready
        try:
            network_ids = [d.get('network_id') for d in focuses_data if d.get('network_id') is not None]
            if not network_ids:
                network_ids = [0]
            self.canvas.compute_palette_for_networks(network_ids)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        from dataclasses import fields as _dataclass_fields

        focus_field_names = {fld.name for fld in _dataclass_fields(Focus)}

        def _coerce_block(value: Any, *, allow_empty: bool) -> Optional[str]:
            if value is None:
                return "" if allow_empty else None
            if isinstance(value, str):
                return value
            if isinstance(value, (list, tuple)):
                joined = "\n".join(str(v) for v in value if v is not None)
                return joined if joined or allow_empty else None
            return str(value)

        def _coerce_list(value: Any) -> List[Any]:
            if value is None:
                return []
            if isinstance(value, list):
                return value
            if isinstance(value, (tuple, set)):
                return list(value)
            return [value]

        block_fields_required = {'description', 'available', 'bypass', 'completion_reward'}
        block_fields_optional = {'visible', 'select_effect', 'remove_effect', 'cancel', 'complete_tooltip'}
        bool_fields = {'prerequisites_grouped', 'available_if_capitulated', 'cancel_if_invalid', 'continue_if_invalid', 'hidden', 'has_unparsed'}
        list_fields = {'raw_unparsed', 'avail_conditions'}

        for f_data in focuses_data:
            try:
                focus_kwargs: Dict[str, Any] = {'id': str(f_data.get('id', '') or '')}
                if not focus_kwargs['id']:
                    continue
                for field_name in focus_field_names:
                    if field_name == 'id' or field_name not in f_data:
                        continue
                    value = f_data[field_name]
                    if field_name in ('x', 'y'):
                        try:
                            value = int(round(float(value)))
                        except Exception:
                            value = 0
                    elif field_name == 'cost':
                        try:
                            value = int(value)
                        except Exception:
                            value = 10
                    elif field_name == 'ai_will_do':
                        try:
                            value = int(value)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    elif field_name == 'network_id':
                        try:
                            value = int(value) if value is not None else None
                        except Exception:
                            value = None
                    elif field_name in block_fields_required:
                        value = _coerce_block(value, allow_empty=True)
                    elif field_name in block_fields_optional:
                        value = _coerce_block(value, allow_empty=False)
                    elif field_name in bool_fields:
                        value = bool(value)
                    elif field_name in list_fields:
                        value = _coerce_list(value)
                    focus_kwargs[field_name] = value
                focus = Focus(**focus_kwargs)
            except Exception as e:
                logger.exception("[loader] failed to construct focus from data: %s; data=%s", e, f_data)
                continue
            try:
                self.focuses.append(focus)
                node = self.canvas.add_focus_node(focus)
                # assign imported category for quick editing later
                try:
                    if getattr(focus, 'hidden', False):
                        focus.imported_category = 'hidden'
                    else:
                        # simple heuristic: if avail_conditions exist, mark as unavailable
                        conds = getattr(focus, 'avail_conditions', []) or []
                        if conds:
                            focus.imported_category = 'unavailable'
                        else:
                            focus.imported_category = 'available'
                except Exception:
                    focus.imported_category = None
                try:
                    focus_id_to_node[focus.id] = node
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                logger.exception("[loader] failed to add focus node to canvas: %s; id=%s", e, getattr(focus, 'id', None))
                show_error(self, "Error", f"Failed to add focus '{focus.id}' to canvas.", exc=e)
                continue
        try:
            self.canvas.refresh_mutex_connectors()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Populate Game State dock (if present) so user can simulate completes
        try:
            if hasattr(self, 'game_state_dock') and self.game_state_dock is not None:
                try:
                    self.game_state_dock.populate(self.focuses)
                    def _on_state_change():
                        # Apply visibility and availability rules based on GameStateDock settings
                        try:
                            gs = self.game_state_dock
                            show_hidden = bool(getattr(gs, 'show_hidden_chk', None) and gs.show_hidden_chk.isChecked())
                            show_unavail = bool(getattr(gs, 'show_unavail_chk', None) and gs.show_unavail_chk.isChecked())
                            show_avail = bool(getattr(gs, 'show_available_chk', None) and gs.show_available_chk.isChecked())
                            for n in list(self.canvas.nodes.values()):
                                try:
                                    f = getattr(n, 'focus', None)
                                    if f is None:
                                        continue
                                    cat = getattr(f, 'imported_category', None)
                                    # Determine simulated availability: if any has_completed_focus is unmet -> unavailable
                                    is_unavailable = False
                                    try:
                                        conds = getattr(f, 'avail_conditions', []) or []
                                        for c in conds:
                                            try:
                                                if c.get('type') == 'has_completed_focus':
                                                    req = str(c.get('value'))
                                                    if not gs.is_completed(req):
                                                        is_unavailable = True
                                                        break
                                            except Exception:
                                                continue
                                    except Exception:
                                        is_unavailable = False

                                    # Decide visibility according to imported category and user toggles
                                    final_visible = True
                                    try:
                                        if cat == 'hidden':
                                            final_visible = show_hidden
                                        elif cat == 'unavailable':
                                            final_visible = show_unavail
                                        elif cat == 'available':
                                            final_visible = show_avail
                                        else:
                                            # default: visible
                                            final_visible = True
                                    except Exception:
                                        final_visible = True

                                    # If node is unavailable but user wants unavailable nodes visible,
                                    # keep visible but set opacity lower via node paint; if user hides them,
                                    # set visible False so they are not drawn.
                                    try:
                                        if is_unavailable and not show_unavail:
                                            try:
                                                n.set_logical_visible(False, user=True)
                                            except Exception:
                                                n.setVisible(False)
                                        else:
                                            try:
                                                n.set_logical_visible(bool(final_visible), user=True)
                                            except Exception:
                                                n.setVisible(bool(final_visible))
                                            # request visual update so paint can apply opacity changes
                                            try:
                                                n.update()
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    except Exception:
                                        try:
                                            n.update()
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        self.game_state_dock.state_changed.connect(_on_state_change)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Refresh Hidden Branches menu now that nodes (and their tags) exist
        try:
            if hasattr(self.canvas, 'refresh_hidden_branches_menu'):
                try:
                    self.canvas.refresh_hidden_branches_menu()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # After adding nodes, compute frames grouping by network
        try:
            self.canvas.update_frames()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Recreate connections (single pass): gather desired parent->child links and create once
        try:
            # Optional diagnostic/repair: detect prerequisites whose declared parent is below the child
            # (this often causes connections to render 'upwards' and look erratic). Enable
            # `self.canvas.auto_fix_prereq_orientation = True` to auto-swap such relations during load.
            try:
                inverted = []
                for focus in self.focuses:
                    try:
                        child_id = getattr(focus, 'id', None)
                        child_node = self.canvas.nodes.get(str(child_id))
                        if child_node is None:
                            continue
                        for parent_id in list(getattr(focus, 'prerequisites', []) or []):
                            try:
                                pnode = self.canvas.nodes.get(str(parent_id))
                                if pnode is None:
                                    continue
                                # If declared parent is visually below child, mark inverted
                                if getattr(pnode, 'focus', None) is not None and getattr(child_node, 'focus', None) is not None:
                                    try:
                                        if float(pnode.focus.y) > float(child_node.focus.y):
                                            inverted.append((str(parent_id), str(child_id)))
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                if inverted:
                    logger.debug("[loader] detected %d inverted prerequisite edges: %s", len(inverted), inverted)
                    if getattr(self.canvas, 'auto_fix_prereq_orientation', False):
                        # Perform conservative swap: make the visually upper node the parent
                        logger.info("[loader] auto-fixing inverted prerequisite orientations (%d edges)", len(inverted))
                        for a, b in inverted:
                            try:
                                # a->b is inverted (a below b). Swap to b->a
                                # remove a from b.prerequisites if present
                                bn = self.canvas.nodes.get(b)
                                an = self.canvas.nodes.get(a)
                                if bn and an:
                                    if a in getattr(bn.focus, 'prerequisites', []):
                                        try:
                                            bn.focus.prerequisites.remove(a)
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    # ensure b present in a.prerequisites
                                    if b not in getattr(an.focus, 'prerequisites', []):
                                        try:
                                            an.focus.prerequisites.append(b)
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            connections_to_create = set()
            for focus in self.focuses:
                try:
                    my_id = getattr(focus, 'id', None)
                    if not my_id:
                        continue
                    # collect kinds from grouped prerequisites
                    parent_kind = {}
                    groups = getattr(focus, 'prerequisites_groups', []) or []
                    for g in groups:
                        try:
                            typ = (g.get('type') or 'AND').upper() if isinstance(g, dict) else 'AND'
                            for pid in list(g.get('items', []) if isinstance(g, dict) else []):
                                if pid and pid not in parent_kind:
                                    parent_kind[pid] = typ
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    # flat prerequisites
                    flat_parents = set(getattr(focus, 'prerequisites', []) or [])
                    all_parents = flat_parents.union(set(parent_kind.keys()))
                    for pid in all_parents:
                        try:
                            if not pid:
                                continue
                            if pid not in getattr(self.canvas, 'nodes', {}):
                                continue
                            # Never create a prerequisite connection for a pair that
                            # is mutually exclusive. Mutex edges are handled by
                            # refresh_mutex_connectors() and must not be treated as
                            # prerequisite links here.
                            try:
                                child_mutex = set(getattr(focus, 'mutually_exclusive', []) or [])
                            except Exception:
                                child_mutex = set()
                            if str(pid) in child_mutex:
                                continue
                            try:
                                pnode = self.canvas.nodes.get(str(pid))
                                p_focus = getattr(pnode, 'focus', None) if pnode is not None else None
                                parent_mutex = set(getattr(p_focus, 'mutually_exclusive', []) or [])
                            except Exception:
                                parent_mutex = set()
                            if str(my_id) in parent_mutex:
                                continue

                            kind = parent_kind.get(pid)
                            connections_to_create.add((str(pid), str(my_id), kind))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Create connections in a single pass, avoiding duplicates
            for from_id, to_id, kind in connections_to_create:
                try:
                    # check for existing visual connection
                    exists = any(
                        getattr(getattr(c, 'start_node', None), 'focus', None) is not None and
                        getattr(getattr(c, 'end_node', None), 'focus', None) is not None and
                        getattr(c.start_node.focus, 'id', None) == from_id and
                        getattr(c.end_node.focus, 'id', None) == to_id
                        for c in list(getattr(self.canvas, 'connections', []))
                    )
                    if not exists:
                        line = self.canvas.create_connection(from_id, to_id, prereq_kind=kind)
                        # create_connection will apply style when prereq_kind provided
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                self.canvas.refresh_mutex_connectors()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Ensure any existing connections reflect group kinds (robust second pass)
        try:
            canvas = getattr(self, 'canvas', None)
            if canvas is not None and hasattr(canvas, 'rebuild_connection_styles'):
                canvas.rebuild_connection_styles(recompute_groups=True, recolor=True, refresh_mutex=False)
            else:
                legacy = None
                if canvas is not None:
                    legacy = getattr(canvas, '_apply_prereq_group_styles', None)
                if legacy is None:
                    legacy = getattr(self, '_apply_prereq_group_styles', None)
                if callable(legacy):
                    legacy()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Enforce hidden-focus visibility after all nodes/connections are created
        try:
            if hasattr(self.canvas, 'apply_hidden_visibility'):
                try:
                    self.canvas.apply_hidden_visibility()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if hasattr(self.canvas, 'schedule_cull'):
                    self.canvas.schedule_cull()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Update frames again in case connections/nodes moved during creation
        try:
            self.canvas.update_frames()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            logger.debug("[loader] canvas nodes after load: %s", list(self.canvas.nodes.keys()))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Build any deferred note→focus links now that focus nodes exist
        try:
            if hasattr(self, '_deferred_note_focus_links') and self._deferred_note_focus_links:
                note_map = {}
                for it in getattr(self.canvas, '_notes_items', []):
                    if getattr(it, 'note_id', None):
                        note_map[it.note_id] = it
                for pair in list(self._deferred_note_focus_links):
                    try:
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            n = note_map.get(str(pair[0]))
                            fnode = self.canvas.nodes.get(str(pair[1]))
                            if n and fnode:
                                self.canvas.add_note_focus_link(n, fnode)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Create events and add to canvas
        try:
            ev_list = list(project.get('events', []) or [])
            norm = []
            for ed in ev_list:
                if not isinstance(ed, dict):
                    continue
                d = dict(ed)
                try:
                    d['x'] = int(float(d.get('x', 0)))
                except Exception:
                    d['x'] = 0
                try:
                    d['y'] = int(float(d.get('y', 0)))
                except Exception:
                    d['y'] = 0
                try:
                    d['free_x'] = None if d.get('free_x', None) is None else float(d.get('free_x'))
                except Exception:
                    d['free_x'] = None
                try:
                    d['free_y'] = None if d.get('free_y', None) is None else float(d.get('free_y'))
                except Exception:
                    d['free_y'] = None
                norm.append(d)
            for d in norm:
                try:
                    ev = Event(id=d.get('id',''), title=d.get('title',''), description=d.get('description',''), x=d.get('x',0), y=d.get('y',0), free_x=d.get('free_x',None), free_y=d.get('free_y',None), trigger=d.get('trigger',''), options_block=d.get('options_block',''))
                    # restore option localisation metadata if present
                    try:
                        ok = d.get('option_keys', None)
                        if isinstance(ok, (list, tuple)):
                            try:
                                ev.option_keys = [str(x) for x in ok if x is not None]
                            except Exception:
                                ev.option_keys = list(ok)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        olv = d.get('option_loc_values', None)
                        if isinstance(olv, dict):
                            clean = {}
                            for kk, vv in olv.items():
                                try:
                                    if kk is None:
                                        continue
                                    clean[str(kk)] = '' if vv is None else str(vv)
                                except Exception:
                                    try:
                                        clean[str(kk)] = ''
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            ev.option_loc_values = clean
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception:
                    continue
                try:
                    self.events.append(ev)
                    self.canvas.add_event_node(ev)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Note→Event links
        try:
            if isinstance(project.get('note_event_links', None), list):
                for pair in list(project.get('note_event_links', [])):
                    try:
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            nid = str(pair[0]); evid = str(pair[1])
                            note_item = None
                            for it in getattr(self.canvas, '_notes_items', []):
                                if getattr(it, 'note_id', None) == nid:
                                    note_item = it; break
                            event_node = getattr(self.canvas, 'event_nodes', {}).get(evid)
                            if note_item and event_node:
                                self.canvas.add_note_event_link(note_item, event_node)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Event↔Focus and Event↔Event links
        try:
            # Build lookup maps
            focus_map = dict(getattr(self.canvas, 'nodes', {}))
            event_map = dict(getattr(self.canvas, 'event_nodes', {}))
            # Event↔Focus
            for pair in list(project.get('event_focus_links', []) or []):
                try:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2:
                        evn = event_map.get(str(pair[0])); fcs = focus_map.get(str(pair[1]))
                        if evn and fcs:
                            self.canvas.add_event_focus_link(evn, fcs)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Event↔Event
            for pair in list(project.get('event_event_links', []) or []):
                try:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2:
                        a = event_map.get(str(pair[0])); b = event_map.get(str(pair[1]))
                        if a and b:
                            self.canvas.add_event_event_link(a, b)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage(f"Loaded generated project ({len(self.focuses)} focuses)")
        self.update_status()
        self.fit_view()
        # Reflow isolates post-load
        try:
            if getattr(self.canvas, 'auto_layout_enabled', False):
                if getattr(self.canvas, '_layout_in_progress', False):
                    pass
                else:
                    if getattr(self.canvas, 'auto_layout_enabled', False):
                        if not self.canvas._reflow_timer.isActive():
                            self.canvas._reflow_timer.start()
        except Exception:
            try:
                if getattr(self.canvas, 'auto_layout_enabled', False):
                    if getattr(self.canvas, 'auto_layout_enabled', False):
                        self.canvas.reflow_unconnected_nodes()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Restore canvas layout flags to their previous values. We intentionally
        # restore after the reflow attempt so disabling above prevented an
        # immediate layout run on load while preserving the user's preference.
        try:
            self.canvas.auto_layout_enabled = prev_auto_layout
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            setattr(self.canvas, '_suspend_layout', prev_suspend)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # -------------------------
    # Export / code-gen
    # -------------------------
    def export_focus_tree(self):
        """Export the focus tree to HOI4 format with project-aware directory selection"""
        if not self.focuses:
            QMessageBox.warning(self, "Warning", "No focuses to export!")
            return

        # Create export dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Export Focus Tree")
        layout = QVBoxLayout(dlg)

        def _mask_user_path(p: str) -> str:
            try:
                return obfuscate_user_in_path(p)
            except Exception:
                return p

        def _resolve_user_path(p: str) -> str:
            if not p:
                return ''
            username = os.environ.get('USERNAME') or os.environ.get('USER') or ''
            if username:
                return p.replace('%USER%', username)
            try:
                home = os.path.expanduser('~')
                base = os.path.basename(home)
                if base:
                    return p.replace('%USER%', base)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return p

        # Get project info
        proj_path = getattr(self, 'current_project_path', None)
        proj_name = None
        if proj_path and os.path.isfile(proj_path):
            proj_name = os.path.splitext(os.path.basename(proj_path))[0]

        # Default export base directory
        abd = getattr(self, 'app_base_dir', None)
        default_base = os.path.join(abd, 'exports') if abd else os.path.join(os.getcwd(), 'exports')
        try:
            os.makedirs(default_base, exist_ok=True)
        except Exception:
            default_base = os.getcwd()

        # Export destination row
        dest_row = QHBoxLayout()
        dest_label = QLabel("Export to:")
        dest_edit = QLineEdit()
        dest_edit.setText(_mask_user_path(default_base))
        browse_btn = QPushButton("Browse...")
        def _browse():
            start_dir = _resolve_user_path(dest_edit.text() or default_base)
            d = QFileDialog.getExistingDirectory(self, "Choose Export Directory", start_dir)
            if d:
                dest_edit.setText(_mask_user_path(d))
        browse_btn.clicked.connect(_browse)
        dest_row.addWidget(dest_label)
        dest_row.addWidget(dest_edit, 1)
        dest_row.addWidget(browse_btn)
        layout.addLayout(dest_row)

        # Project options group
        proj_group = QGroupBox("Project Export Options")
        proj_layout = QVBoxLayout(proj_group)

        use_project_cb = QCheckBox("Create project subfolder")
        use_project_cb.setChecked(True if proj_name else False)
        use_project_cb.setEnabled(bool(proj_name))
        if proj_name:
            use_project_cb.setToolTip(f"Export to: {default_base}/{proj_name}/common/national_focus/")
        else:
            use_project_cb.setToolTip("No project loaded - files will be exported directly to chosen folder")

        mod_struct_cb = QCheckBox("Use HOI4 mod structure (common/national_focus)")
        mod_struct_cb.setChecked(True)
        mod_struct_cb.setEnabled(True if proj_name and use_project_cb.isChecked() else False)

        unique_cb = QCheckBox("Auto-rename if files exist (add _1, _2, etc.)")
        unique_cb.setChecked(False)

        def _toggle_mod_struct():
            mod_struct_cb.setEnabled(use_project_cb.isChecked() and bool(proj_name))

        use_project_cb.toggled.connect(_toggle_mod_struct)

        proj_layout.addWidget(use_project_cb)
        proj_layout.addWidget(mod_struct_cb)
        proj_layout.addWidget(unique_cb)
        layout.addWidget(proj_group)

        # Files to export group
        files_group = QGroupBox("Files to Export")
        files_layout = QVBoxLayout(files_group)

        focus_cb = QCheckBox("Focus tree (.txt)")
        focus_cb.setChecked(True)
        focus_cb.setEnabled(False)  # Always required

        gfx_cb = QCheckBox("GFX files (goals.gfx, goals_shine.gfx)")
        gfx_cb.setChecked(True)

        events_cb = QCheckBox("Events file (.txt)")
        events_cb.setChecked(True)
        if not getattr(self, 'events', None):
            events_cb.setEnabled(False)
            events_cb.setToolTip("No events in project")

        files_layout.addWidget(focus_cb)
        files_layout.addWidget(gfx_cb)
        files_layout.addWidget(events_cb)
        layout.addWidget(files_group)

        # Preview label
        preview_label = QLabel("")
        preview_label.setWordWrap(True)
        preview_label.setStyleSheet("QLabel { color: gray; font-style: italic; padding: 5px; }")

        def _update_preview():
            base = dest_edit.text() or _mask_user_path(default_base)
            if use_project_cb.isChecked() and proj_name:
                if mod_struct_cb.isChecked():
                    preview = f"→ {base}/{proj_name}/common/national_focus/..."
                else:
                    preview = f"→ {base}/{proj_name}/..."
            else:
                preview = f"→ {base}/..."
            preview_label.setText(preview)

        dest_edit.textChanged.connect(_update_preview)
        use_project_cb.toggled.connect(_update_preview)
        mod_struct_cb.toggled.connect(_update_preview)
        _update_preview()

        layout.addWidget(preview_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        export_btn = QPushButton("Export")
        export_btn.setDefault(True)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(export_btn)
        layout.addLayout(btn_layout)

        cancel_btn.clicked.connect(dlg.reject)

        def _do_export():
            try:
                base_dir = _resolve_user_path(dest_edit.text().strip() or _mask_user_path(default_base))
                use_project = use_project_cb.isChecked() and bool(proj_name)
                use_mod_struct = mod_struct_cb.isChecked() and use_project
                ensure_unique = unique_cb.isChecked()

                # Build tree ID for filename
                tree_id = str(getattr(self, 'tree_id', '') or 'focus_tree')

                # Determine output path
                if use_project:
                    if use_mod_struct:
                        output_dir = os.path.join(base_dir, proj_name, 'common', 'national_focus')
                    else:
                        output_dir = os.path.join(base_dir, proj_name)
                else:
                    output_dir = base_dir

                os.makedirs(output_dir, exist_ok=True)

                # Helper for unique paths
                def _unique_path(path: str) -> str:
                    if not ensure_unique or not os.path.exists(path):
                        return path
                    base, ext = os.path.splitext(path)
                    counter = 1
                    while True:
                        candidate = f"{base}_{counter}{ext}"
                        if not os.path.exists(candidate):
                            return candidate
                        counter += 1

                created_files = []

                # 1) Export focus tree
                import re
                safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', tree_id).strip('_') or 'focus_tree'
                focus_path = _unique_path(os.path.join(output_dir, f"{safe_tree_id}.txt"))
                with open(focus_path, 'w', encoding='utf-8') as f:
                    f.write(self.generate_hoi4_code())
                created_files.append(os.path.basename(focus_path))

                # 2) Export GFX files if requested
                if gfx_cb.isChecked():
                    try:
                        icon_idents = self._collect_unique_icon_idents()
                        if icon_idents:
                            gfx_dir = os.path.join(base_dir, proj_name, 'gfx', 'interface', 'goals') if (use_project and use_mod_struct) else output_dir
                            os.makedirs(gfx_dir, exist_ok=True)

                            goals_path = _unique_path(os.path.join(gfx_dir, 'goals.gfx'))
                            with open(goals_path, 'w', encoding='utf-8') as f:
                                f.write(self._generate_goals_gfx_content(icon_idents))
                            created_files.append(os.path.basename(goals_path))

                            shine_path = _unique_path(os.path.join(gfx_dir, 'goals_shine.gfx'))
                            with open(shine_path, 'w', encoding='utf-8') as f:
                                f.write(self._generate_goals_shine_gfx_content(icon_idents))
                            created_files.append(os.path.basename(shine_path))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # 3) Export events if requested
                if events_cb.isChecked() and getattr(self, 'events', None):
                    try:
                        events_dir = os.path.join(base_dir, proj_name, 'events') if (use_project and use_mod_struct) else output_dir
                        os.makedirs(events_dir, exist_ok=True)

                        events_path = _unique_path(os.path.join(events_dir, f"{safe_tree_id}_events.txt"))
                        with open(events_path, 'w', encoding='utf-8') as f:
                            f.write(self.generate_events_txt())
                        created_files.append(os.path.basename(events_path))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Success message
                if use_project:
                    result_msg = f"Exported to project folder:\n{obfuscate_path(os.path.join(base_dir, proj_name))}\n\nFiles created: {', '.join(created_files)}"
                else:
                    result_msg = f"Exported to:\n{obfuscate_path(output_dir)}\n\nFiles created: {', '.join(created_files)}"

                QMessageBox.information(self, "Export Successful", result_msg)
                self.statusBar().showMessage(f"Exported {len(created_files)} file(s)")
                dlg.accept()

            except Exception as e:
                show_error(self, "Export Failed", "Failed to export focus tree.", exc=e)

        export_btn.clicked.connect(_do_export)

        dlg.setLayout(layout)
        dlg.resize(600, 0)
        dlg.exec()

    # -------------------------
    # Export Panel: full/modular export including localisation
    # -------------------------
    def open_export_panel(self) -> None:
        """Open a dialog to export specific files or the whole mod at once, including localisation."""
        if not getattr(self, 'focuses', None):
            QMessageBox.warning(self, "Export", "No focuses to export!")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Export")
        layout = QVBoxLayout(dlg)

        def _mask_user_path(p: str) -> str:
            try:
                return obfuscate_user_in_path(p)
            except Exception:
                return p

        def _resolve_user_path(p: str) -> str:
            if not p:
                return ''
            username = os.environ.get('USERNAME') or os.environ.get('USER') or ''
            if username:
                return p.replace('%USER%', username)
            try:
                home = os.path.expanduser('~')
                base = os.path.basename(home)
                if base:
                    return p.replace('%USER%', base)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return p

        # Get project info for display
        proj_path = getattr(self, 'current_project_path', None)
        proj_name = None
        if proj_path and os.path.isfile(proj_path):
            proj_name = os.path.splitext(os.path.basename(proj_path))[0]

        # Destination folder chooser
        dest_row = QHBoxLayout()
        dest_label = QLabel("Destination folder:")
        dest_edit = QLineEdit()
        # Default to app exports folder or cwd
        default_dir = None
        try:
            abd = getattr(self, 'app_base_dir', None)
            if abd:
                default_dir = os.path.join(abd, 'exports')
                os.makedirs(default_dir, exist_ok=True)
        except Exception:
            default_dir = None
        if not default_dir:
            default_dir = os.getcwd()
            dest_edit.setText(_mask_user_path(default_dir))
        browse_btn = QPushButton("Browse…")
        def _browse():
            start_dir = _resolve_user_path(dest_edit.text() or default_dir or os.getcwd())
            d = QFileDialog.getExistingDirectory(self, "Choose destination", start_dir)
            if d:
                    dest_edit.setText(_mask_user_path(d))
        browse_btn.clicked.connect(_browse)
        dest_row.addWidget(dest_label)
        dest_row.addWidget(dest_edit, 1)
        dest_row.addWidget(browse_btn)
        layout.addLayout(dest_row)

        # Project info label (if project is open)
        if proj_name:
            proj_info = QLabel(f"Project: <b>{proj_name}</b>")
            proj_info.setStyleSheet("QLabel { color: #4a9eff; padding: 5px; }")
            layout.addWidget(proj_info)

        # Options
        opts_group = QGroupBox("Files to export")
        opts_layout = QVBoxLayout(opts_group)
        cb_focus = QCheckBox("Focus tree (.txt)")
        cb_focus.setChecked(True)
        cb_goals = QCheckBox("GFX goals.gfx")
        cb_goals.setChecked(True)
        cb_shine = QCheckBox("GFX goals_shine.gfx")
        cb_shine.setChecked(True)
        cb_events = QCheckBox("Events (.txt)")
        cb_events.setChecked(True)
        if not getattr(self, 'events', None):
            try:
                cb_events.setEnabled(False)
                cb_events.setToolTip("No events in project")
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        cb_loc = QCheckBox("Localisation (.yml)")
        cb_loc.setChecked(True)
        cb_states = QCheckBox("States (.txt)")
        cb_states.setChecked(False)
        opts_layout.addWidget(cb_focus)
        opts_layout.addWidget(cb_goals)
        opts_layout.addWidget(cb_shine)
        opts_layout.addWidget(cb_events)
        opts_layout.addWidget(cb_loc)
        opts_layout.addWidget(cb_states)
        layout.addWidget(opts_group)

        # Localisation languages input (comma-separated)
        lang_row = QHBoxLayout()
        lang_label = QLabel("Localisation languages:")
        lang_edit = QLineEdit()
        lang_edit.setPlaceholderText("english,german,french,spanish,russian,polish,japanese,braz_por,simp_chinese")
        lang_edit.setText("english")
        lang_row.addWidget(lang_label)
        lang_row.addWidget(lang_edit, 1)
        layout.addLayout(lang_row)
        # Enable/disable language input based on loc checkbox
        def _toggle_langs():
            lang_label.setEnabled(cb_loc.isChecked())
            lang_edit.setEnabled(cb_loc.isChecked())
        cb_loc.toggled.connect(_toggle_langs)
        _toggle_langs()

        # Structure option
        struct_cb = QCheckBox("Create mod folder structure (common/national_focus, gfx/interface/goals, localisation/<lang>)")
        struct_cb.setChecked(True)
        layout.addWidget(struct_cb)

        # Preview label showing final export path
        preview_label = QLabel("")
        preview_label.setWordWrap(True)
        preview_label.setStyleSheet("QLabel { color: gray; font-style: italic; padding: 5px; background: #2a2a2a; border-radius: 3px; }")

        def _update_preview():
            base = dest_edit.text() or _mask_user_path(default_dir)
            if proj_name:
                import re
                safe_proj = re.sub(r'[^A-Za-z0-9_\-]+', '_', proj_name).strip('_') or 'project'
                final_path = os.path.join(base, safe_proj)
                obfuscated_path = obfuscate_path(final_path)
                if struct_cb.isChecked():
                    preview_label.setText(f"→ Exports to: {obfuscated_path}/common/national_focus/... (+ other mod folders)")
                else:
                    preview_label.setText(f"→ Exports to: {obfuscated_path}/ (flat structure)")
            else:
                obfuscated_base = obfuscate_path(base)
                if struct_cb.isChecked():
                    preview_label.setText(f"→ Exports to: {obfuscated_base}/common/national_focus/... (+ other mod folders)")
                else:
                    preview_label.setText(f"→ Exports to: {obfuscated_base}/ (flat structure)")

        dest_edit.textChanged.connect(_update_preview)
        struct_cb.toggled.connect(_update_preview)
        _update_preview()

        layout.addWidget(preview_label)

        # Buttons
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        export_btn = QPushButton("Export")
        btns.addWidget(cancel_btn)
        btns.addWidget(export_btn)
        layout.addLayout(btns)

        cancel_btn.clicked.connect(dlg.reject)
        def _do_export():
            base_dir = _resolve_user_path(dest_edit.text().strip() or _mask_user_path(default_dir) or os.getcwd())
            # parse languages list (comma-separated), normalise to hoi4 language keys
            raw_langs = (lang_edit.text() or "english") if cb_loc.isChecked() else ""
            lang_items = [x.strip() for x in raw_langs.split(',') if x.strip()]
            # Normalisation map for common aliases
            alias_map = {
                'en': 'english', 'eng': 'english', 'english': 'english',
                'de': 'german', 'ger': 'german', 'german': 'german',
                'fr': 'french', 'fre': 'french', 'fra': 'french', 'french': 'french',
                'es': 'spanish', 'spa': 'spanish', 'spanish': 'spanish',
                'ru': 'russian', 'rus': 'russian', 'russian': 'russian',
                'pl': 'polish', 'pol': 'polish', 'polish': 'polish',
                'jp': 'japanese', 'jpn': 'japanese', 'japanese': 'japanese',
                'pt': 'braz_por', 'pt-br': 'braz_por', 'br': 'braz_por', 'brazilian': 'braz_por', 'braz_por': 'braz_por',
                'cn': 'simp_chinese', 'zh': 'simp_chinese', 'zh-cn': 'simp_chinese', 'chinese': 'simp_chinese', 'simp_chinese': 'simp_chinese',
            }
            loc_languages = []
            for l in lang_items:
                key = alias_map.get(l.lower(), l.lower())
                if key and key not in loc_languages:
                    loc_languages.append(key)
            if cb_loc.isChecked() and not loc_languages:
                loc_languages = ['english']
            try:
                self._export_with_options(
                    base_dir=base_dir,
                    do_focus=cb_focus.isChecked(),
                    do_goals=cb_goals.isChecked(),
                    do_shine=cb_shine.isChecked(),
                    do_events=cb_events.isChecked(),
                    do_loc=cb_loc.isChecked(),
                    do_states=cb_states.isChecked(),
                    loc_languages=loc_languages,
                    mod_structure=struct_cb.isChecked(),
                )
                dlg.accept()
            except Exception as e:
                show_error(self, "Export Failed", "Export failed.", exc=e)

        export_btn.clicked.connect(_do_export)

        dlg.setLayout(layout)
        dlg.resize(640, 0)
        dlg.exec()

    def _export_with_options(self, base_dir: str, do_focus: bool, do_goals: bool, do_shine: bool, do_events: bool, do_loc: bool, mod_structure: bool, loc_languages: Optional[List[str]] = None, do_states: bool = False) -> None:
        """Execute export based on options selected in the Export panel."""
        created: List[str] = []

        # Check if we have a project open - if so, create project-specific subfolder
        proj_path = getattr(self, 'current_project_path', None)
        proj_name = None
        if proj_path and os.path.isfile(proj_path):
            proj_name = os.path.splitext(os.path.basename(proj_path))[0]
            # Create project subfolder
            import re
            safe_proj_name = re.sub(r'[^A-Za-z0-9_\-]+', '_', proj_name).strip('_') or 'project'
            base_dir = os.path.join(base_dir, safe_proj_name)

        os.makedirs(base_dir, exist_ok=True)

        # Resolve output paths depending on structure flag
        def p_join(*parts: str) -> str:
            return os.path.join(*parts)

        try:
            if mod_structure:
                focus_dir = p_join(base_dir, 'common', 'national_focus')
                gfx_dir = p_join(base_dir, 'gfx', 'interface', 'goals')
                loc_dir = p_join(base_dir, 'localisation', 'english')
                events_dir = p_join(base_dir, 'events')
            else:
                focus_dir = base_dir
                gfx_dir = base_dir
                loc_dir = base_dir
                events_dir = base_dir
            for d in (focus_dir, gfx_dir, loc_dir, events_dir):
                try:
                    os.makedirs(d, exist_ok=True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # 1) Focus tree
            if do_focus:
                # Sanitize tree_id for filename
                try:
                    import re
                    safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', str(self.tree_id or '')).strip('_') or 'focus_tree'
                except Exception:
                    safe_tree_id = str(self.tree_id or 'focus_tree')
                focus_fn = os.path.join(focus_dir, f"{safe_tree_id}.txt")
                with open(focus_fn, 'w', encoding='utf-8') as f:
                    f.write(self.generate_hoi4_code())
                created.append(os.path.relpath(focus_fn, base_dir))

            # Collect unique icon identifiers once
            icon_idents: List[str] = []
            try:
                icon_idents = self._collect_unique_icon_idents()
            except Exception:
                icon_idents = []

            # 2) goals.gfx
            if do_goals and icon_idents:
                goals_fn = os.path.join(gfx_dir, 'goals.gfx')
                with open(goals_fn, 'w', encoding='utf-8') as gf:
                    gf.write(self._generate_goals_gfx_content(icon_idents))
                created.append(os.path.relpath(goals_fn, base_dir))

            # 3) goals_shine.gfx
            if do_shine and icon_idents:
                shine_fn = os.path.join(gfx_dir, 'goals_shine.gfx')
                with open(shine_fn, 'w', encoding='utf-8') as sf:
                    sf.write(self._generate_goals_shine_gfx_content(icon_idents))
                created.append(os.path.relpath(shine_fn, base_dir))

            # 3.5) events
            if do_events and getattr(self, 'events', None):
                # Sanitize tree_id for filename
                try:
                    import re
                    safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', str(self.tree_id or '')).strip('_') or 'focus_tree'
                except Exception:
                    safe_tree_id = str(self.tree_id or 'focus_tree')
                ev_fn = os.path.join(events_dir, f"{safe_tree_id}_events.txt")
                with open(ev_fn, 'w', encoding='utf-8') as ef:
                    ef.write(self.generate_events_txt())
                created.append(os.path.relpath(ev_fn, base_dir))

            # 4) localisation .yml
            if do_loc:
                langs = loc_languages or ['english']
                for lang in langs:
                    # Determine output dir per language
                    out_loc_dir = loc_dir
                    if mod_structure:
                        out_loc_dir = os.path.join(base_dir, 'localisation', lang)
                        try:
                            os.makedirs(out_loc_dir, exist_ok=True)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    loc_fn = os.path.join(out_loc_dir, f"focus_l_{lang}.yml")
                    with open(loc_fn, 'w', encoding='utf-8-sig') as lf:
                        lf.write(self.generate_localisation_yml(language=lang))
                    created.append(os.path.relpath(loc_fn, base_dir))
                    # Also export events localisation file if events are present
                    try:
                        events_list = getattr(self, 'events', []) or []
                        if events_list:
                            ev_loc_fn = os.path.join(out_loc_dir, f"events_l_{lang}.yml")
                            with open(ev_loc_fn, 'w', encoding='utf-8-sig') as evlf:
                                evlf.write(self.generate_events_localisation_yml(language=lang))
                            created.append(os.path.relpath(ev_loc_fn, base_dir))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    # 5) States (.txt)
                    if do_states:
                        try:
                            if mod_structure:
                                states_dir = os.path.join(base_dir, 'history', 'states')
                            else:
                                states_dir = os.path.join(base_dir, 'states')
                            os.makedirs(states_dir, exist_ok=True)
                            # If we have a StateViewportDock, delegate to its exporter for fidelity
                            try:
                                sv = getattr(self, 'state_viewport_dock', None)
                                if sv is not None and getattr(sv, '_state_meta', None):
                                    succ, fail = sv._export_states_to_dir(sv._state_meta, states_dir)
                                else:
                                    # fallback: try to use HOI4StateExporter directly
                                    from _exporter import HOI4StateExporter
                                    exporter = HOI4StateExporter()
                                    succ, fail = exporter.export_states_batch(getattr(sv, '_state_meta', {}) or {}, states_dir)
                            except Exception:
                                # If no state viewport available or exporter fails, write nothing
                                succ = 0; fail = 0
                            if succ:
                                created.append(os.path.relpath(states_dir, base_dir))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Notify
            if created:
                QMessageBox.information(self, "Export", f"Exported {len(created)} file(s) to:\n{obfuscate_path(base_dir)}\n\n" + "\n".join(created))
                try:
                    self.statusBar().showMessage(f"Exported: {', '.join(created)}")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                QMessageBox.information(self, "Export", "Nothing selected to export.")
        except Exception as e:
            raise

    def generate_localisation_yml(self, language: str = 'english') -> str:
        """Generate localisation content for focus titles and descriptions in HOI4 .yml format.

        Format:
        l_<language>:
         <FOCUS_ID>:0 "<Title from GUI>"
         <FOCUS_ID>_desc:0 "<Description from GUI>"
        """
        lang_key = f"l_{language.lower()}"

        def esc_title(s: str) -> str:
            """Escape double-quotes for YAML/simple HOI4 loc values."""
            try:
                return (s or "").replace('"', '\\"')
            except Exception:
                return s or ""

        def esc_desc(s: str) -> str:
            """Escape and normalise description text (encode newlines as \\n)."""
            try:
                s = s or ""
                s = s.replace("\r\n", "\n").replace("\r", "\n")
                s = s.replace('"', '\\"').replace("\n", "\\n")
                return s
            except Exception:
                return s or ""

        lines: List[str] = [f"{lang_key}:"]
        seen_keys: Set[str] = set()
        # Sorted by y, x like code export to keep determinism
        for f in sorted(self.focuses, key=lambda v: (v.y, v.x)):
            raw_id = getattr(f, 'id', None) or ""  # should exist
            fid = str(raw_id).strip() or "FOCUS_KEY"
            title = getattr(f, 'title', None) or getattr(f, 'name', None) or getattr(f, 'focus_title', None) or fid
            title_key = fid
            if title_key not in seen_keys:
                lines.append(f" {title_key}:0 \"{esc_title(str(title))}\"")
                seen_keys.add(title_key)
            # Add description localisation if provided
            desc_val = getattr(f, 'description', None)
            if isinstance(desc_val, str) and desc_val.strip():
                desc_key = f"{fid}_desc"
                if desc_key not in seen_keys:
                    lines.append(f" {desc_key}:0 \"{esc_desc(desc_val)}\"")
                    seen_keys.add(desc_key)
        return "\n".join(lines) + "\n"

    def generate_events_localisation_yml(self, language: str = 'english') -> str:
        """Generate localisation file content only for events (titles, descs, option localisations).

        This yields the HOI4-style localisation block `l_<language>:` containing only event entries.
        """
        lang_key = f"l_{language.lower()}"

        def esc_title(s: str) -> str:
            try:
                return (s or "").replace('"', '\\"')
            except Exception:
                return s or ""

        def esc_desc(s: str) -> str:
            try:
                s = s or ""
                s = s.replace("\r\n", "\n").replace("\r", "\n")
                s = s.replace('"', '\\"').replace("\n", "\\n")
                return s
            except Exception:
                return s or ""

        lines: List[str] = [f"{lang_key}:"]
        seen_keys: Set[str] = set()
        try:
            events_list = getattr(self, 'events', []) or []
            def _e_key(e):
                try:
                    return (int(getattr(e, 'y', 0)), int(getattr(e, 'x', 0)), str(getattr(e, 'id', '')))
                except Exception:
                    return (0, 0, str(getattr(e, 'id', '')))
            for ev in sorted(events_list, key=_e_key):
                evid = str(getattr(ev, 'id', '') or '').strip()
                if not evid:
                    continue
                title_val = getattr(ev, 'title', '') or evid
                desc_val = getattr(ev, 'description', '') or ''
                t_key = f"{evid}.t"
                d_key = f"{evid}.d"
                if t_key not in seen_keys:
                    lines.append(f" {t_key}:0 \"{esc_title(str(title_val))}\"")
                    seen_keys.add(t_key)
                if desc_val and d_key not in seen_keys:
                    lines.append(f" {d_key}:0 \"{esc_desc(str(desc_val))}\"")
                    seen_keys.add(d_key)
                # option localisations
                try:
                    vals = getattr(ev, 'option_loc_values', None) or {}
                    keys = getattr(ev, 'option_keys', None) or (list(vals.keys()) if isinstance(vals, dict) else [])
                    if isinstance(keys, (list, tuple)) and keys:
                        for k in keys:
                            try:
                                if not k:
                                    continue
                                opt_val = vals.get(k, '')
                                if opt_val is None:
                                    opt_val = ''
                                opt_key = str(k)
                                if opt_key not in seen_keys:
                                    lines.append(f" {opt_key}:0 \"{esc_title(str(opt_val))}\"")
                                    seen_keys.add(opt_key)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return "\n".join(lines) + "\n"

    def generate_events_txt(self) -> str:
        """Generate HOI4 events file content for all events in the project."""
        lines: List[str] = []
        w = lines.append
        w(f"# Events for {self.tree_id}")
        w("")
        # sort by id for determinism, but prefer y,x if available
        def _key(e):
            try:
                return (int(getattr(e, 'y', 0)), int(getattr(e, 'x', 0)), str(getattr(e, 'id', '')))
            except Exception:
                return (0, 0, str(getattr(e, 'id', '')))
        for ev in sorted(getattr(self, 'events', []) or [], key=_key):
            # Use the single-block formatter so any injected event→focus wiring is applied
            try:
                blk = str(self.format_event_block(ev, base_indent=1) or '')
                # format_event_block returns lines without an extra trailing blank line; keep a blank line between events
                for l in blk.splitlines():
                    w(l)
                w("")
            except Exception:
                # Fallback to previous minimal format if formatting fails for any event
                evid = str(getattr(ev, 'id', '') or '').strip() or 'event.1'
                w("country_event = {")
                w(f"\tid = {evid}")
                w(f"\ttitle = {evid}.t")
                w(f"\tdesc = {evid}.d")
                w("\tis_triggered_only = yes")
                w(f"\t# TODO: Add options for {evid}")
                w(f"\toption = {{ name = {evid}.a }}")
                w("}\n")
        return "\n".join(lines) + "\n"

    def generate_hoi4_code(self):
        """Generate HOI4 focus tree code with improved formatting"""
        lines: List[str] = []

        # Ensure focus positions reflect canvas before exporting
        try:
            if hasattr(self, 'canvas') and getattr(self.canvas, 'sync_focus_positions', None):
                self.canvas.sync_focus_positions()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        def w(line: str = "", level: int = 0) -> None:
            lines.append(("\t" * level) + line)

        def esc_str(s: str) -> str:
            # Escape double-quotes and backslashes for safe embedding
            return s.replace("\\", "\\\\").replace('"', '\\"')

        def safe_ident(ident: str) -> str:
            # If identifier is a simple token (alnum + underscore), leave unquoted for HOI4
            if not ident:
                return '""'
            try:
                if isinstance(ident, str) and re.match(r"^[A-Za-z0-9_]+$", ident):
                    return ident
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return '"' + esc_str(ident) + '"'

        import datetime, re
        w(f"# Generated by HOI4 Focus GUI - {datetime.datetime.utcnow().isoformat()}Z")
        w()
        w("focus_tree = {")
        w(f"id = {safe_ident(str(self.tree_id))}", 1)
        w("country = {", 1)
        w("factor = 0", 2)
        w("modifier = {", 2)
        w("add = 10", 3)
        w(f"tag = {safe_ident(str(self.country_tag))}", 3)
        w("}", 2)
        w("}", 1)
        w("default = no", 1)
        w("reset_on_civilwar = no", 1)
        w()

        # Sort focuses top-to-bottom then left-to-right
        sorted_focuses = sorted(self.focuses, key=lambda f: (f.y, f.x))

        # Compute a small export offset so exported coordinates are non-negative
        # (HOI4 prefers coordinates starting near 0 and negative coords can shift
        # the entire tree unexpectedly when loaded). We do not mutate the
        # in-memory Focus objects; the offset is only applied to exported values.
        if self.focuses:
            _xs = [getattr(f, 'x', 0) for f in self.focuses]
            _ys = [getattr(f, 'y', 0) for f in self.focuses]
            _min_x = min(_xs)
            _min_y = min(_ys)
            _offset_x = -_min_x if _min_x < 0 else 0
            _offset_y = -_min_y if _min_y < 0 else 0
        else:
            _offset_x = 0
            _offset_y = 0

        for focus in sorted_focuses:
            # Use the single-block formatter which includes the event-linked availability logic
            try:
                blk = str(self.format_focus_block(focus, base_indent=1) or '')
                for l in blk.splitlines():
                    w(l)
                w("")
            except Exception:
                # Fallback: write a minimal block if formatting fails
                w("focus = {", 1)
                w(f"id = {safe_ident(str(focus.id))}", 2)
                w("}", 1)
                w("")
            w()

        w("}")
        return "\n".join(lines) + "\n"

    def format_focus_block(self, focus: Focus, base_indent: int = 0) -> str:
        """Render a single focus block using the same ordering/formatting as generate_hoi4_code().

        The output mirrors the content of one iteration of generate_hoi4_code, without the surrounding
        focus_tree header/footer. Indentation starts at `base_indent` tabs for the 'focus = {' line.
        """
        lines: List[str] = []

        def w(line: str = "", level: int = 0) -> None:
            lines.append(("\t" * (base_indent + level)) + line)

        def esc_str(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        def safe_ident(ident: str) -> str:
            if not ident:
                return '""'
            try:
                import re as _re
                if isinstance(ident, str) and _re.match(r"^[A-Za-z0-9_]+$", ident):
                    return ident
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return '"' + esc_str(ident) + '"'

        # Begin focus block
        w("focus = {")
        w(f"id = {safe_ident(str(focus.id))}", 1)

        # icon mapping identical to exporter
        icon_val = getattr(focus, 'icon', None)
        if icon_val:
            try:
                s = str(icon_val)
                if any(sep in s for sep in (os.path.sep, '/', '\\')):
                    base = os.path.splitext(os.path.basename(s))[0]
                    ident = base
                else:
                    ident = s
                    try:
                        path = (self.icon_library or {}).get(s)
                        if path and any(sep in path for sep in (os.path.sep, '/', '\\')):
                            ident = os.path.splitext(os.path.basename(path))[0]
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception:
                ident = str(icon_val)
            sprite_name = ident if str(ident).startswith('GFX_') else f"GFX_{ident}"
            w(f"icon = {safe_ident(sprite_name)}", 1)

        # prerequisites: if richer `prerequisites_groups` exists, use it. Otherwise fall back to legacy `prerequisites`.
        try:
            groups = getattr(focus, 'prerequisites_groups', None) or []
        except Exception:
            groups = []
        if groups:
            # Iterate groups in order. Each group is a dict {'type': 'AND'|'OR', 'items': [...]}
            for g in groups:
                try:
                    gtype = (g.get('type', 'AND') or 'AND').upper()
                    items = list(g.get('items', []) or [])
                    # Clean items
                    items = [str(it).strip() for it in items if str(it).strip()]
                    if not items:
                        continue
                    if gtype == 'OR':
                        # OR semantics: single prerequisite block containing multiple focus entries
                        w("prerequisite = {", 1)
                        for it in items:
                            w(f"focus = {safe_ident(str(it))}", 2)
                        w("}", 1)
                    else:
                        # AND semantics: emit one prerequisite block per item
                        for it in items:
                            w("prerequisite = {", 1)
                            w(f"focus = {safe_ident(str(it))}", 2)
                            w("}", 1)
                except Exception:
                    continue
        else:
            # legacy behavior
            if focus.prerequisites:
                try:
                    grouped = bool(getattr(focus, 'prerequisites_grouped', False))
                except Exception:
                    grouped = False
                if grouped:
                    # Render grouped prerequisites as a single multi-line block
                    w("prerequisite = {", 1)
                    for prereq in focus.prerequisites:
                        w(f"focus = {safe_ident(str(prereq))}", 2)
                    w("}", 1)
                else:
                    for prereq in focus.prerequisites:
                        w("prerequisite = {", 1)
                        w(f"focus = {safe_ident(str(prereq))}", 2)
                        w("}", 1)

        w()
        # position and cost (include relative_position_id when available)
        # Apply same non-mutating export offset as generate_hoi4_code so
        # single-block rendering matches full export.
        try:
            if self.focuses:
                _xs = [getattr(f, 'x', 0) for f in self.focuses]
                _ys = [getattr(f, 'y', 0) for f in self.focuses]
                _min_x = min(_xs)
                _min_y = min(_ys)
                _offset_x = -_min_x if _min_x < 0 else 0
                _offset_y = -_min_y if _min_y < 0 else 0
            else:
                _offset_x = 0
                _offset_y = 0
        except Exception:
            _offset_x = 0
            _offset_y = 0
        if focus.prerequisites:
            prereq_id = focus.prerequisites[0]
            prereq = next((f for f in self.focuses if f.id == prereq_id), None)
            if prereq:
                # Emit coordinates relative to the prerequisite when possible.
                try:
                    dx = int(round(float(getattr(focus, 'x', 0)) - float(getattr(prereq, 'x', 0))))
                    dy = int(round(float(getattr(focus, 'y', 0)) - float(getattr(prereq, 'y', 0))))
                    w(f"x = {dx}", 1)
                    w(f"y = {dy}", 1)
                except Exception:
                    w(f"x = {int(focus.x + _offset_x)}", 1)
                    w(f"y = {int(focus.y + _offset_y)}", 1)
                w(f"relative_position_id = {safe_ident(str(prereq.id))}", 1)
            else:
                w(f"x = {int(focus.x + _offset_x)}", 1)
                w(f"y = {int(focus.y + _offset_y)}", 1)
        else:
            w(f"x = {int(focus.x + _offset_x)}", 1)
            w(f"y = {int(focus.y + _offset_y)}", 1)
        w(f"cost = {int(getattr(focus, 'cost', 10))}", 1)

        w()
        # search_filters block
        sf = getattr(focus, 'search_filters', None)
        if isinstance(sf, (list, tuple, set)):
            sf_values = [str(s) for s in sf if s is not None and str(s) != '']
        elif sf:
            sf_values = [str(sf)]
        else:
            sf_values = []

        if not sf_values:
            w("search_filters = { ", 1)
            w("}", 1)
        else:
            inner = "  ".join(sf_values)
            w(f"search_filters = {{ {inner}  }}", 1)

        # available_if_capitulated flag
        if getattr(focus, 'available_if_capitulated', False):
            w("available_if_capitulated = yes", 1)

        w()
        # ai_will_do
        ai_factor = getattr(focus, 'ai_will_do', None)
        w("ai_will_do = {", 1)
        if ai_factor is not None and int(ai_factor) != 1:
            w(f"factor = {int(ai_factor)}", 2)
        w("}", 1)

        w()
        # allow_branch block (optional branch visibility gating)
        if getattr(focus, 'allow_branch', None):
            w("allow_branch = {", 1)
            for line in str(focus.allow_branch).strip().split('\n'):
                t = line.rstrip()
                if t:
                    w(t, 2)
            w("}", 1)
            w()

        # available / bypass / complete_tooltip blocks
        # available / bypass / complete_tooltip blocks
        foc_avail = getattr(focus, 'available', None)
        if foc_avail:
            w("available = {", 1)
            for line in str(foc_avail).strip().split('\n'):
                t = line.rstrip()
                if t:
                    w(t, 2)
            w("}", 1)
        else:
            # No explicit available condition: emit an empty available block.
            w("available = {", 1)
            w("}", 1)

        w()
        if getattr(focus, 'bypass', None):
            w("bypass = {", 1)
            for line in str(focus.bypass).strip().split('\n'):
                t = line.rstrip()
                if t:
                    w(t, 2)
            w("}", 1)
        else:
            w("bypass = {", 1)
            w("}", 1)

        w()
        if getattr(focus, 'complete_tooltip', None):
            w("complete_tooltip = {", 1)
            for line in str(focus.complete_tooltip).strip().split('\n'):
                t = line.rstrip()
                if t:
                    w(t, 2)
            w("}", 1)
        else:
            w("complete_tooltip = {", 1)
            w("}", 1)

        w()
        # completion_reward
        if getattr(focus, 'completion_reward', None):
            w("completion_reward = {", 1)
            for line in str(focus.completion_reward).strip().split('\n'):
                t = line.rstrip()
                if t:
                    w(t, 2)
            w("}", 1)
        else:
            # If the focus has no explicit completion_reward but is linked to one or more Events,
            # add a completion_reward that triggers those events immediately using days=0.
            try:
                linked = []
                canvas = getattr(self, 'canvas', None)
                if canvas is not None:
                    for ef in getattr(canvas, '_event_focus_links', []) or []:
                        ev = getattr(getattr(ef, 'event_node', None), 'event', None)
                        fn = getattr(getattr(ef, 'focus_node', None), 'focus', None)
                        if ev is not None and fn is not None and getattr(fn, 'id', None) == getattr(focus, 'id', None):
                            linked.append(str(getattr(ev, 'id', '') or ''))
                if linked:
                    w("completion_reward = {", 1)
                    for eid in linked:
                        try:
                            # Use safe_ident to quote identifiers when needed
                            safe = eid
                            try:
                                import re as _re
                                if _re.match(r"^[A-Za-z0-9_.]+$", str(eid)):
                                    safe = str(eid)
                                else:
                                    # quote if contains spaces or odd chars
                                    safe = '"' + str(eid).replace('\\', '\\\\').replace('"', '\\"') + '"'
                            except Exception:
                                safe = '"' + str(eid).replace('\\', '\\\\').replace('"', '\\"') + '"'
                            w(f"country_event = {{ id = {safe} days = 0 }}", 2)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    w("}", 1)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # mutually exclusive entries
        if focus.mutually_exclusive:
            for mutex in focus.mutually_exclusive:
                w(f"mutually_exclusive = {{ focus = {safe_ident(str(mutex))} }}", 1)

        w("}")
        return "\n".join(lines)

    def format_event_block(self, ev: Event, base_indent: int = 0) -> str:
        """Render a single event block using the same ordering/formatting as generate_events_txt()."""
        lines: List[str] = []

        def w(line: str = "", level: int = 0) -> None:
            lines.append(("\t" * (base_indent + level)) + line)

        evid = str(getattr(ev, 'id', '') or '').strip() or 'event.1'
        w("country_event = {")
        w(f"id = {evid}", 1)
        w(f"title = {evid}.t", 1)
        w(f"desc = {evid}.d", 1)
        w("is_triggered_only = yes", 1)

    # Trigger block logic
        trig = str(getattr(ev, 'trigger', '') or '').strip()
        if trig:
            t_lines = [l.rstrip() for l in trig.splitlines()]
            try:
                first_chunk = t_lines[0].lower() if t_lines else ''
            except Exception:
                first_chunk = ''
            if first_chunk.startswith('trigger') or '{' in trig or '=' in first_chunk:
                for tl in t_lines:
                    if tl:
                        w(tl, 1)
                    else:
                        w("", 1)
            else:
                if len(t_lines) == 1:
                    w(f"trigger = {{ {t_lines[0].strip()} }}", 1)
                else:
                    w("trigger = {", 1)
                    for tl in t_lines:
                        w(tl, 2)
                    w("}", 1)
        else:
            w(f"# trigger: add conditions for {evid} here (or edit in Event editor)", 1)

        # If this event is a parent of one or more focuses, ensure it will not auto-fire
        # and should be treated as triggered-only by the game.
        try:
            canvas = getattr(self, 'canvas', None)
            linked_focus_ids_check = []
            if canvas is not None:
                for ef in getattr(canvas, '_event_focus_links', []) or []:
                    evn = getattr(getattr(ef, 'event_node', None), 'event', None)
                    fn = getattr(getattr(ef, 'focus_node', None), 'focus', None)
                    if evn is not None and fn is not None and str(getattr(evn, 'id', '')) == evid:
                        linked_focus_ids_check.append(str(getattr(fn, 'id', '') or ''))
            if linked_focus_ids_check:
                w("fire_only_once = yes", 1)
                w("is_triggered_only = yes", 1)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Options block
        opts = str(getattr(ev, 'options_block', '') or '').rstrip()
        if opts:
            # If this event is linked to focuses, we will try to inject a
            # small set of effect lines into each option block so that choosing
            # an option sets a country flag indicating the event fired for
            # that focus (and optionally triggers the focus via country_event).
            try:
                canvas = getattr(self, 'canvas', None)
                linked_focus_ids = []
                if canvas is not None:
                    for ef in getattr(canvas, '_event_focus_links', []) or []:
                        evn = getattr(getattr(ef, 'event_node', None), 'event', None)
                        fn = getattr(getattr(ef, 'focus_node', None), 'focus', None)
                        if evn is not None and fn is not None and str(getattr(evn, 'id', '')) == evid:
                            fid = str(getattr(fn, 'id', '') or '')
                            if fid:
                                linked_focus_ids.append(fid)
                # Prepare injection snippet for each option (if any linked focuses)
                inject_lines = []
                if linked_focus_ids:
                    # Helper to safely quote identifiers
                    def _safe_ident_local(ident: str) -> str:
                        if not ident:
                            return '""'
                        try:
                            import re as _re
                            if isinstance(ident, str) and _re.match(r"^[A-Za-z0-9_]+$", ident):
                                return ident
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # escape quotes/backslashes
                        s = str(ident).replace('\\', '\\\\').replace('"', '\\"')
                        return '"' + s + '"'

                    # Use one event-based flag per event to indicate it fired
                    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", evid)
                    inject_lines.append(f"set_country_flag = {sanitized}_fired")
                    # Also trigger each linked focus (user requested option to directly trigger focus)
                    for fid in linked_focus_ids:
                        if fid:
                            inject_lines.append(f"country_event = {{ id = {_safe_ident_local(str(fid))} }}")
                # If injection is needed, try to locate option blocks and append effects
                if inject_lines:
                    # More robust heuristic: detect 'option' lines and track brace depth
                    out_lines = []
                    in_option = False
                    brace_depth = 0
                    for ol in opts.splitlines():
                        if ol is None:
                            continue
                        stripped = ol.rstrip()
                        lstrip = stripped.lstrip()
                        # Detect an 'option' start (handles 'option = {' and 'option =', possibly with name on next line)
                        if lstrip.startswith('option') and not in_option:
                            # Enter option tracking; count any braces on this line
                            in_option = True
                            brace_depth = stripped.count('{') - stripped.count('}')
                            out_lines.append(stripped)
                            # If the option opened and closed on the same line, inject immediately after
                            if brace_depth <= 0:
                                for il in inject_lines:
                                    out_lines.append('\t' + il)
                                in_option = False
                                brace_depth = 0
                            continue
                        if in_option:
                            # update brace depth
                            brace_depth += stripped.count('{') - stripped.count('}')
                            # If we reach the end of the option block (brace_depth <= 0), inject before closing
                            if brace_depth <= 0 and stripped.strip().endswith('}'):
                                for il in inject_lines:
                                    out_lines.append('\t' + il)
                                out_lines.append(stripped)
                                in_option = False
                                brace_depth = 0
                                continue
                            out_lines.append(stripped)
                            continue
                        out_lines.append(stripped)
                    # If heuristic failed (no option braces matched), simply append a new option with injections
                    if not any(l.strip().startswith('option') for l in opts.splitlines()):
                        blk = ['option = {', '\tname = ' + evid + '.a']
                        for il in inject_lines:
                            blk.append('\t' + il)
                        blk.append('}')
                        out_lines.extend(blk)
                    for ol in out_lines:
                        w(ol, 1)
                else:
                    for ol in opts.splitlines():
                        if ol is None:
                            continue
                        w(ol.rstrip(), 1)
            except Exception:
                for ol in opts.splitlines():
                    if ol is None:
                        continue
                    w(ol.rstrip(), 1)
        else:
            w(f"# TODO: Add options for {evid}", 1)
            w(f"option = {{ name = {evid}.a }}", 1)

        w("}")
        return "\n".join(lines)

    # -------------------------
    # GFX helpers for goals and shine definitions
    # -------------------------
    def _collect_unique_icon_idents(self) -> List[str]:
        """Collect unique icon identifiers from focuses.

        Resolution rules:
        - If focus.icon is a file path, use its basename without extension
        - Else treat it as a library key/identifier as-is
        Results are returned sorted for stable output.
        """
        idents = set()
        try:
            for f in self.focuses:
                icon_val = getattr(f, 'icon', None)
                if not icon_val:
                    continue
                s = str(icon_val)
                ident = None
                try:
                    # If the value is a path, take the filename stem
                    if any(sep in s for sep in (os.path.sep, '/', '\\')):
                        ident = os.path.splitext(os.path.basename(s))[0]
                    else:
                        # Could be a library key; if library maps to a file, prefer its basename for consistency
                        path = None
                        try:
                            path = (self.icon_library or {}).get(s)
                        except Exception:
                            path = None
                        if path and any(sep in path for sep in (os.path.sep, '/', '\\')):
                            ident = os.path.splitext(os.path.basename(path))[0]
                        else:
                            ident = s
                except Exception:
                    ident = s
                if ident:
                    idents.add(ident)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return sorted(idents)

    def _generate_goals_gfx_content(self, idents: List[str]) -> str:
        """Generate content for goals.gfx with one SpriteType per unique icon.

        Format example requested:
        spriteTypes = {
        SpriteType = {
        name = "GFX_focus_PLACEHOLDER"
        texturefile = "gfx/interface/goals/focus_PLACEHOLDER.dds"
        }
        }
        Our implementation generalizes to ident being the full basename (e.g., focus_my_icon)
        resulting in name "GFX_<ident>" and texturefile "gfx/interface/goals/<ident>.dds".
        """
        lines: List[str] = []
        lines.append("spriteTypes = {")
        for ident in idents:
            lines.append("\tSpriteType = {")
            lines.append(f"\t\tname = \"GFX_{ident}\"")
            lines.append(f"\t\ttexturefile = \"gfx/interface/goals/{ident}.dds\"")
            lines.append("\t}")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _generate_goals_shine_gfx_content(self, idents: List[str]) -> str:
        """Generate content for goals_shine.gfx with one SpriteType per unique icon, matching the intended HOI4 shine format."""
        lines: List[str] = []
        lines.append("spriteTypes = {")
        for ident in idents:
            lines.append("\tSpriteType = {")
            lines.append(f"\t\tname = \"GFX_{ident}_shine\"")
            lines.append(f"\t\ttexturefile = \"gfx/interface/goals/{ident}.dds\"")
            lines.append(f"\t\teffectFile = \"gfx/FX/buttonstate.lua\"")
            # First animation block
            lines.append(f"\t\tanimation = {{")
            lines.append(f"\t\t\tanimationmaskfile = \"gfx/interface/goals/{ident}.dds\"")
            lines.append(f"\t\t\tanimationtexturefile = \"gfx/interface/goals/shine_overlay.dds\"")
            lines.append(f"\t\t\tanimationrotation = -90.0")
            lines.append(f"\t\t\tanimationlooping = no")
            lines.append(f"\t\t\tanimationtime = 0.75")
            lines.append(f"\t\t\tanimationdelay = 0")
            lines.append(f"\t\t\tanimationblendmode = \"add\"")
            lines.append(f"\t\t\tanimationtype = \"scrolling\"")
            lines.append(f"\t\t\tanimationrotationoffset = {{ x = 0.0 y = 0.0 }}")
            lines.append(f"\t\t\tanimationtexturescale = {{ x = 2.0 y = 1.0 }}")
            lines.append(f"\t\t}}")
            # Second animation block
            lines.append(f"\t\tanimation = {{")
            lines.append(f"\t\t\tanimationmaskfile = \"gfx/interface/goals/{ident}.dds\"")
            lines.append(f"\t\t\tanimationtexturefile = \"gfx/interface/goals/shine_overlay.dds\"")
            lines.append(f"\t\t\tanimationrotation = 90.0")
            lines.append(f"\t\t\tanimationlooping = no")
            lines.append(f"\t\t\tanimationtime = 0.75")
            lines.append(f"\t\t\tanimationdelay = 0")
            lines.append(f"\t\t\tanimationblendmode = \"add\"")
            lines.append(f"\t\t\tanimationtype = \"scrolling\"")
            lines.append(f"\t\t\tanimationrotationoffset = {{ x = 0.0 y = 0.0 }}")
            lines.append(f"\t\t\tanimationtexturescale = {{ x = 1.0 y = 1.0 }}")
            lines.append(f"\t\t}}")
            lines.append(f"\t\tlegacy_lazy_load = no")
            lines.append("\t}")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # -------------------------
    # Save / load project
    # -------------------------
    def _build_project_payload(self) -> Dict[str, Any]:
        """Assemble the current project state into a serialisable dict."""
        project: Dict[str, Any] = {
            'version': getattr(self, 'app_version', '1.0.9'),
            'tree_id': getattr(self, 'tree_id', ''),
            'country_tag': getattr(self, 'country_tag', ''),
            'focuses': [],
            'events': [],
            'library': getattr(self, 'library', {}),
            'icon_library': getattr(self, 'icon_library', {}),
            'settings': {
                'canvas': self.canvas.to_settings() if hasattr(self, 'canvas') and getattr(self.canvas, 'to_settings', None) else {},
                'preferences': {
                    'prefer_app_settings': bool(getattr(self, 'prefer_app_settings', False))
                },
            },
            'note_defaults': getattr(self.canvas, 'note_defaults', {}) if hasattr(self, 'canvas') else {},
            'notes': [],
            'note_connections': [],
            'note_focus_links': [],
            'note_event_links': [],
            'event_focus_links': [],
            'event_event_links': [],
        }
        try:
            if hasattr(self, 'canvas') and getattr(self.canvas, 'sync_focus_positions', None):
                self.canvas.sync_focus_positions()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        for focus in getattr(self, 'focuses', []) or []:
            try:
                entry = {'id': focus.id}
                # Required/minimal fields
                if getattr(focus, 'name', None):
                    entry['name'] = focus.name
                # Only include coordinates if not zero to save space; loader will default to 0.0
                try:
                    if float(getattr(focus, 'x', 0.0)) != 0.0:
                        entry['x'] = focus.x
                except Exception:
                    entry['x'] = focus.x
                try:
                    if float(getattr(focus, 'y', 0.0)) != 0.0:
                        entry['y'] = focus.y
                except Exception:
                    entry['y'] = focus.y
                # Only include non-default/meaningful metadata
                if getattr(focus, 'cost', None) is not None and getattr(focus, 'cost', None) != 0:
                    entry['cost'] = focus.cost
                if getattr(focus, 'description', ''):
                    entry['description'] = focus.description
                if getattr(focus, 'prerequisites', None):
                    entry['prerequisites'] = focus.prerequisites
                if getattr(focus, 'mutually_exclusive', None):
                    entry['mutually_exclusive'] = focus.mutually_exclusive
                if getattr(focus, 'prerequisites_grouped', False):
                    entry['prerequisites_grouped'] = True
                if getattr(focus, 'prerequisites_groups', None):
                    pg = getattr(focus, 'prerequisites_groups', []) or []
                    if pg:
                        entry['prerequisites_groups'] = pg
                if getattr(focus, 'search_filters', None):
                    entry['search_filters'] = list(getattr(focus, 'search_filters', []))
                if getattr(focus, 'available', None):
                    entry['available'] = focus.available
                if getattr(focus, 'visible', None):
                    entry['visible'] = focus.visible
                if getattr(focus, 'bypass', None):
                    entry['bypass'] = focus.bypass
                if getattr(focus, 'completion_reward', None):
                    entry['completion_reward'] = focus.completion_reward
                if getattr(focus, 'select_effect', None):
                    entry['select_effect'] = focus.select_effect
                if getattr(focus, 'remove_effect', None):
                    entry['remove_effect'] = focus.remove_effect
                if getattr(focus, 'cancel', None):
                    entry['cancel'] = focus.cancel
                if getattr(focus, 'complete_tooltip', None):
                    entry['complete_tooltip'] = focus.complete_tooltip
                # ai_will_do default is 1; only store if different
                try:
                    if int(getattr(focus, 'ai_will_do', 1)) != 1:
                        entry['ai_will_do'] = int(getattr(focus, 'ai_will_do', 1))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                if getattr(focus, 'ai_will_do_block', None):
                    entry['ai_will_do_block'] = getattr(focus, 'ai_will_do_block', None)
                if getattr(focus, 'allow_branch', None):
                    entry['allow_branch'] = getattr(focus, 'allow_branch', None)
                if getattr(focus, 'network_id', None) is not None:
                    entry['network_id'] = getattr(focus, 'network_id', None)
                if getattr(focus, 'relative_position_id', None):
                    entry['relative_position_id'] = getattr(focus, 'relative_position_id', None)
                if getattr(focus, 'available_if_capitulated', False):
                    entry['available_if_capitulated'] = True
                if getattr(focus, 'cancel_if_invalid', False):
                    entry['cancel_if_invalid'] = True
                if getattr(focus, 'continue_if_invalid', False):
                    entry['continue_if_invalid'] = True
                if getattr(focus, 'will_lead_to_war_with', None):
                    entry['will_lead_to_war_with'] = list(getattr(focus, 'will_lead_to_war_with', []))
                if getattr(focus, 'icon', None):
                    entry['icon'] = getattr(focus, 'icon', None)
                if getattr(focus, 'hidden', False):
                    entry['hidden'] = True
                if getattr(focus, 'hidden_tags', None):
                    entry['hidden_tags'] = list(getattr(focus, 'hidden_tags', []))
                if getattr(focus, 'avail_conditions', None):
                    entry['avail_conditions'] = list(getattr(focus, 'avail_conditions', []))
                if getattr(focus, 'raw_unparsed', None):
                    entry['raw_unparsed'] = list(getattr(focus, 'raw_unparsed', []))
                if getattr(focus, 'has_unparsed', False):
                    entry['has_unparsed'] = True
                if getattr(focus, 'clean_raw', None):
                    entry['clean_raw'] = getattr(focus, 'clean_raw', None)
                project['focuses'].append(entry)
            except Exception as exc:
                logger.warning('Failed to serialize focus %s: %s', getattr(focus, 'id', None), exc)

        if hasattr(self, 'canvas'):
            for note in getattr(self.canvas, '_notes_items', []) or []:
                try:
                    if hasattr(note, 'to_dict'):
                        project['notes'].append(note.to_dict())
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for conn in getattr(self.canvas, '_note_connections', []) or []:
                try:
                    project['note_connections'].append({
                        'a': getattr(conn.a, 'note_id', ''),
                        'b': getattr(conn.b, 'note_id', ''),
                        'label': getattr(conn, 'label', '') or '',
                        'manual_offset': float(getattr(conn, 'manual_offset', 0.0) or 0.0),
                        'manual_angle': None if getattr(conn, 'manual_angle', None) is None else float(getattr(conn, 'manual_angle', None)),
                    })
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            project['note_focus_links'] = [
                [
                    getattr(link.note, 'note_id', ''),
                    (getattr(getattr(link, 'focus_node', None), 'focus', None).id if getattr(getattr(link, 'focus_node', None), 'focus', None) else '')
                ]
                for link in getattr(self.canvas, '_note_focus_links', []) or []
            ]
            project['note_event_links'] = [
                [
                    getattr(link.note, 'note_id', ''),
                    (getattr(getattr(link, 'event_node', None), 'event', None).id if getattr(getattr(link, 'event_node', None), 'event', None) else '')
                ]
                for link in getattr(self.canvas, '_note_event_links', []) or []
            ]
            project['event_focus_links'] = [
                [
                    (getattr(getattr(link, 'event_node', None), 'event', None).id if getattr(getattr(link, 'event_node', None), 'event', None) else ''),
                    (getattr(getattr(link, 'focus_node', None), 'focus', None).id if getattr(getattr(link, 'focus_node', None), 'focus', None) else '')
                ]
                for link in getattr(self.canvas, '_event_focus_links', []) or []
            ]
            project['event_event_links'] = [
                [
                    (getattr(getattr(link, 'a', None), 'event', None).id if getattr(getattr(link, 'a', None), 'event', None) else ''),
                    (getattr(getattr(link, 'b', None), 'event', None).id if getattr(getattr(link, 'b', None), 'event', None) else '')
                ]
                for link in getattr(self.canvas, '_event_event_links', []) or []
            ]

        for ev in getattr(self, 'events', []) or []:
            try:
                option_keys = [str(k) for k in (getattr(ev, 'option_keys', []) or [])]
            except Exception:
                option_keys = []
            opt_loc_values: Dict[str, str] = {}
            try:
                raw_vals = getattr(ev, 'option_loc_values', {}) or {}
                if isinstance(raw_vals, dict):
                    for kk, vv in raw_vals.items():
                        opt_loc_values[str(kk)] = '' if vv is None else str(vv)
            except Exception:
                opt_loc_values = {}
            ev_entry = {'id': ev.id}
            if getattr(ev, 'title', None):
                ev_entry['title'] = ev.title
            if getattr(ev, 'description', None):
                ev_entry['description'] = ev.description
            try:
                if int(getattr(ev, 'x', 0) or 0) != 0:
                    ev_entry['x'] = int(getattr(ev, 'x', 0) or 0)
            except Exception:
                ev_entry['x'] = int(getattr(ev, 'x', 0) or 0)
            try:
                if int(getattr(ev, 'y', 0) or 0) != 0:
                    ev_entry['y'] = int(getattr(ev, 'y', 0) or 0)
            except Exception:
                ev_entry['y'] = int(getattr(ev, 'y', 0) or 0)
            if getattr(ev, 'free_x', None) is not None:
                ev_entry['free_x'] = float(ev.free_x)
            if getattr(ev, 'free_y', None) is not None:
                ev_entry['free_y'] = float(ev.free_y)
            if getattr(ev, 'trigger', None):
                ev_entry['trigger'] = ev.trigger
            if getattr(ev, 'options_block', None):
                ev_entry['options_block'] = ev.options_block
            if option_keys:
                ev_entry['option_keys'] = option_keys
            if opt_loc_values:
                ev_entry['option_loc_values'] = opt_loc_values
            project['events'].append(ev_entry)
    # Include any loaded/edited state data from the State Viewport so
        # projects persist state edits alongside focuses and events.
        try:
            if hasattr(self, 'state_viewport_dock') and getattr(self, 'state_viewport_dock', None) is not None:
                try:
                    if getattr(self.state_viewport_dock, '_serialize_state_payload', None):
                        state_payload = self.state_viewport_dock._serialize_state_payload() or {}
                        # Merge keys (e.g. 'states', 'provinces') into project root
                        for k, v in (state_payload.items() if isinstance(state_payload, dict) else []):
                            try:
                                project[k] = v
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Persist keybindings into project settings when the user does NOT prefer app-wide settings.
        try:
            try:
                prefer_app = bool(getattr(self, 'prefer_app_settings', False))
            except Exception:
                prefer_app = False
            if not prefer_app and getattr(self, 'keybinds', None) is not None:
                try:
                    kb_map = self.keybinds.get_mapping() or {}
                    if kb_map:
                        project.setdefault('settings', {}).setdefault('keybindings', dict(kb_map))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        return project
    def save_project(self):
        """Save project with enhanced error handling"""
        # If a project was previously loaded/saved, prefer overwriting that path
        filename = None
        if getattr(self, 'current_project_path', None):
            # Only trust current path if it's a JSON file; otherwise force Save As
            try:
                cpp = str(self.current_project_path)
                if cpp.lower().endswith('.json'):
                    filename = cpp
            except Exception:
                filename = None
        else:
            # Use the same folder resolution as Projects dialog for consistency
            default_dir = getattr(self, 'projects_home_path', None)
            if not default_dir:
                abd = getattr(self, 'app_base_dir', None)
                if abd:
                    default_dir = os.path.join(abd, 'projects')
            if not default_dir:
                default_dir = os.getcwd()
            try:
                os.makedirs(default_dir, exist_ok=True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Sanitize tree_id to create a safe filename
            try:
                import re
                raw_tree_id = str(getattr(self, 'tree_id', '') or '')
                safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', raw_tree_id).strip('_') or 'project'
            except Exception:
                safe_tree_id = getattr(self, 'tree_id', 'project') or 'project'

            default_path = os.path.join(default_dir, f"{safe_tree_id}.json")
            filename, _ = QFileDialog.getSaveFileName(self, "Save Project", default_path, "JSON Files (*.json)")

        # If the filename was chosen by getSaveFileName it may be relative — ensure correct default directory
        if filename:
            # Enforce .json extension if user omitted it or selected a non-json filter
            try:
                if not str(filename).lower().endswith('.json'):
                    filename = f"{filename}.json"
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                project = self._build_project_payload()
                # Ensure destination folder exists
                try:
                    os.makedirs(os.path.dirname(filename) or '.', exist_ok=True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # If the State Viewport has an external map loaded, persist it as a sidecar
                try:
                    if hasattr(self, 'state_viewport_dock') and getattr(self.state_viewport_dock, '_serialize_state_payload', None):
                        state_payload = self.state_viewport_dock._serialize_state_payload() or {}
                    else:
                        state_payload = None
                except Exception:
                    state_payload = None
                try:
                    # Only write sidecar if the State Viewport requests it (checkbox)
                    should_persist_sidecar = True
                    try:
                        if hasattr(self, 'state_viewport_dock'):
                            should_persist_sidecar = bool(getattr(self.state_viewport_dock, 'persist_sidecar', True))
                    except Exception:
                        should_persist_sidecar = True
                    if state_payload and isinstance(state_payload, dict) and should_persist_sidecar:
                        proj_dir = os.path.dirname(filename) or '.'
                        base = os.path.splitext(os.path.basename(filename))[0]
                        sidecar_name = f"{base}.states.json"
                        sidecar_path = os.path.join(proj_dir, sidecar_name)
                        tmp_side = sidecar_path + '.tmp'
                        try:
                            if _write_state_sidecar:
                                _write_state_sidecar(sidecar_path, state_payload)
                            else:
                                with open(tmp_side, 'w', encoding='utf-8') as sf:
                                    json.dump(state_payload, sf, ensure_ascii=False, indent=2)
                                try:
                                    os.replace(tmp_side, sidecar_path)
                                except Exception:
                                    try:
                                        shutil.copy2(tmp_side, sidecar_path)
                                        os.remove(tmp_side)
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception:
                            # best-effort fallback
                            try:
                                with open(tmp_side, 'w', encoding='utf-8') as sf:
                                    json.dump(state_payload, sf, ensure_ascii=False, indent=2)
                                try:
                                    os.replace(tmp_side, sidecar_path)
                                except Exception:
                                    shutil.copy2(tmp_side, sidecar_path)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        # record relative sidecar name so loader can find it
                        try:
                            project.setdefault('settings', {}).setdefault('state_viewport', {})['last_map'] = sidecar_name
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # If we recorded a per-project sidecar, avoid embedding duplicate state data
                try:
                    sv = project.get('settings', {}) or {}
                    last_map = (sv.get('state_viewport') or {}).get('last_map')
                    if last_map:
                        for k in ('states', 'provinces'):
                            try:
                                if k in project:
                                    del project[k]
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                atomic_write_json(filename, project)
                try:
                    if not bool(getattr(self, 'muted', False)):
                        try:
                            QMessageBox.information(self, "Success", f"Project saved to {obfuscate_path(filename)}")
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    if hasattr(self, 'statusBar') and self.statusBar() is not None:
                        self.statusBar().showMessage(f"Saved to {obfuscate_path(filename)}")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    if hasattr(self, '_notify_save'):
                        try:
                            self._notify_save()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # remember current project path
                try:
                    self.current_project_path = filename
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            except Exception as e:
                show_error(self, "Error", "Failed to save.", exc=e)

    def save_project_and_settings(self):
        """Compatibility wrapper used by QAction trigger: call save_project (which now also writes separate app settings)."""
        try:
            self.save_project()
            # Also persist app-level settings so preferences carry across projects
            try:
                self.save_settings()
                try:
                    if hasattr(self, '_notify_save'):
                        try:
                            self._notify_save()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            show_error(self, "Save Error", "Failed to save project and settings.", exc=e)

    def scan_icon_library(self):
        """Populate self.icon_library by scanning the configured icon_library_path for .tga/.dds files."""
        try:
            path = getattr(self, 'icon_library_path', None) or ''
            if not path:
                return
            self.icon_library.clear()
            if not os.path.isdir(path):
                return
            for fn in sorted(os.listdir(path)):
                if fn.lower().endswith(('.tga', '.dds')):
                    key = os.path.splitext(fn)[0]
                    # ensure unique keys
                    base = key
                    i = 1
                    while base in self.icon_library:
                        base = f"{key}_{i}"
                        i += 1
                    self.icon_library[base] = os.path.join(path, fn)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def load_project(self):
        """Load a project JSON file and render it using the unified loader."""
        # Start in the consistent projects folder
        start_dir = getattr(self, 'projects_home_path', None)
        if not start_dir:
            abd = getattr(self, 'app_base_dir', None)
            if abd:
                start_dir = os.path.join(abd, 'projects')
        if not start_dir:
            start_dir = ""
        filename, _ = QFileDialog.getOpenFileName(self, "Load Project", start_dir, "JSON Files (*.json)")
        if not filename:
            return
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                project = json.load(f)
        except Exception as e:
            show_error(self, "Error", "Failed to read project file.", exc=e)
            return

        # Apply metadata early
        try:
            # If the project contains an explicit preference for app-settings precedence,
            # honor it (this will be read again inside load_project_from_dict as well).
            try:
                ps = project.get('settings', {}) or {}
                prefs = ps.get('preferences', {}) or {}
                self.prefer_app_settings = bool(prefs.get('prefer_app_settings', getattr(self, 'prefer_app_settings', False)))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            self.tree_id = project.get('tree_id', 'custom_focus_tree')
            self.tree_id_edit.setText(self.tree_id)
            self.country_tag = project.get('country_tag', 'TAG')
            self.country_edit.setText(self.country_tag)
            if 'library' in project and isinstance(project['library'], dict):
                self.library = project['library']
                self.refresh_library_list()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Delegate to the robust loader (handles normalization, frames, connections, fit)
        self.load_project_from_dict(project)

        # Remember current path for subsequent saves
        try:
            self.current_project_path = filename
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # User feedback
        try:
            n = len(project.get('focuses', []) or [])
            if n == 0:
                QMessageBox.information(self, "Loaded", f"Project loaded from {obfuscate_path(filename)}\nNote: This project contains 0 focuses.")
            else:
                QMessageBox.information(self, "Loaded", f"Project loaded from {obfuscate_path(filename)} ({n} focuses)")
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.statusBar().showMessage(f"Loaded {obfuscate_path(filename)}")
        self.update_status()

    # -------------------------
    # Library management
    # -------------------------
    def refresh_library_list(self):
        """Rebuild library overview (folder cards) and prepare per-folder trees.

        The library panel now shows a card overview of folders. Clicking a folder
        card opens a larger per-folder tree view (stack). This function builds
        the overview and (lazily) populates folder trees when opened.
        """
        try:
            q = (self.lib_search.text() or "").strip().lower()

            # Gather items into tuples (key, name, entry, category)
            entries = []
            for key, entry in self.library.items():
                try:
                    if isinstance(entry, dict):
                        name = entry.get('name') or entry.get('id') or key
                        category = entry.get('category') or entry.get('group') or 'Ungrouped'
                    else:
                        name = str(entry)
                        category = 'Ungrouped'
                    entries.append((key, name, entry, category))
                except Exception as e:
                    logger.warning("Skipping corrupted library entry '%s': %s", key, e)
                    continue

            # Filter entries by search
            if q:
                entries = [t for t in entries if q in t[0].lower() or q in t[1].lower() or q in t[3].lower()]

            # Build category counts
            cat_counts: Dict[str, int] = {}
            for _, _, _, category in entries:
                cat_counts[category] = cat_counts.get(category, 0) + 1

            # Reduce number of visible categories: keep top N by alphabet then merge rest into 'Other'
            MAX_CATEGORIES = 8
            cats = sorted(cat_counts.keys())
            if len(cats) > MAX_CATEGORIES:
                visible = cats[:MAX_CATEGORIES-1]
                others = cats[MAX_CATEGORIES-1:]
                visible.append('Other')
            else:
                visible = cats

            # Clear previous overview widgets
            # Remove all widgets from overview layout
            for i in reversed(range(self.lib_overview_layout.count())):
                w = self.lib_overview_layout.itemAt(i).widget()
                if w:
                    w.setParent(None)

            # Clear existing per-folder trees (they will be recreated lazily)
            # Keep the overview (stack index 0) and remove other stack widgets
            while self.lib_stack.count() > 1:
                w = self.lib_stack.widget(1)
                self.lib_stack.removeWidget(w)
                try:
                    w.deleteLater()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self._folder_trees.clear()

            # Layout folder cards in a grid
            cols = 2
            row = 0
            col = 0
            for cat in visible:
                if cat == 'Other':
                    # Count others
                    count = sum(cat_counts.get(c, 0) for c in cats if c not in visible)
                else:
                    count = cat_counts.get(cat, 0)

                card = QFrame()
                card.setFrameShape(QFrame.Shape.StyledPanel)
                card.setMinimumHeight(80)
                card_layout = QVBoxLayout()
                title = QLabel(f"{cat}")
                title.setStyleSheet("font-weight: bold;")
                count_lbl = QLabel(f"{count} entries")
                open_btn = QPushButton("Open")
                open_btn.setToolTip(f"Open folder {cat}")
                open_btn.clicked.connect(lambda chk, c=cat: self.open_folder_view(c))
                card_layout.addWidget(title)
                card_layout.addWidget(count_lbl)
                card_layout.addStretch()
                card_layout.addWidget(open_btn)
                card.setLayout(card_layout)

                self.lib_overview_layout.addWidget(card, row, col)
                col += 1
                if col >= cols:
                    col = 0
                    row += 1

            # If no categories, show placeholder
            if not cats:
                placeholder = QLabel("(No entries)")
                placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.lib_overview_layout.addWidget(placeholder, 0, 0)

        except Exception as e:
            logger.exception("Error refreshing library overview")

    def get_active_folder_tree(self):
        """Return the currently visible QTreeWidget for a folder, or None if overview is shown."""
        try:
            idx = self.lib_stack.currentIndex()
            if idx <= 0:
                return None
            widget = self.lib_stack.widget(idx)
            if not widget:
                return None
            tree = widget.findChild(QTreeWidget)
            return tree
        except Exception:
            return None

    def open_folder_view(self, category_name: str):
        """Open the per-folder tree for `category_name`. Creates it lazily if necessary."""
        try:
            # If category already has a tree, switch to it
            if category_name in self._folder_trees:
                widget = self._folder_trees[category_name]['container']
                self.lib_stack.setCurrentWidget(widget)
                return

            # Create container with back button and tree
            container = QWidget()
            v = QVBoxLayout()
            header_layout = QHBoxLayout()
            back_btn = QPushButton("Back")
            back_btn.clicked.connect(lambda: self.lib_stack.setCurrentIndex(0))
            header_layout.addWidget(back_btn)
            header_layout.addWidget(QLabel(f"Folder: {category_name}"))
            header_layout.addStretch()
            v.addLayout(header_layout)

            tree = QTreeWidget()
            tree.setColumnCount(1)
            tree.setHeaderHidden(True)
            tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            tree.itemDoubleClicked.connect(self.on_folder_tree_double_clicked)
            tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tree.customContextMenuRequested.connect(self.show_library_context_menu)
            v.addWidget(tree)
            container.setLayout(v)

            # Populate the tree with entries for this category
            self.populate_folder_tree(category_name, tree)

            # Add to stack and mapping
            self.lib_stack.addWidget(container)
            self._folder_trees[category_name] = {'container': container, 'tree': tree}
            self.lib_stack.setCurrentWidget(container)
        except Exception as e:
            logger.exception("Error opening folder view '%s'", category_name)

    def populate_folder_tree(self, category_name: str, tree: QTreeWidget):
        """Populate a QTreeWidget with entries from `self.library` matching `category_name`.

        If `category_name` is 'Other', include entries whose categories were merged.
        """
        try:
            tree.clear()
            q = (self.lib_search.text() or "").strip().lower()

            # Collect entries for this category
            items = []
            for key, entry in self.library.items():
                try:
                    if isinstance(entry, dict):
                        name = entry.get('name') or entry.get('id') or key
                        category = entry.get('category') or entry.get('group') or 'Ungrouped'
                    else:
                        name = str(entry)
                        category = 'Ungrouped'
                    items.append((key, name, entry, category))
                except Exception:
                    continue

            # Determine which categories were visible in overview
            MAX_CATEGORIES = 8
            cats = sorted({it[3] for it in items})
            if len(cats) > MAX_CATEGORIES:
                visible = cats[:MAX_CATEGORIES-1]
                others = set(cats[MAX_CATEGORIES-1:])
            else:
                visible = cats
                others = set()

            # Filter items matching this category view
            rows = []
            for key, name, entry, category in items:
                if category_name == 'Other':
                    if category in others:
                        rows.append((key, name, entry, category))
                else:
                    if category == category_name:
                        rows.append((key, name, entry, category))

            # Sort rows by name
            rows.sort(key=lambda t: (t[1].lower(), t[0].lower()))

            for key, name, entry, category in rows:
                display = f"{name} [{key}]"
                child = QTreeWidgetItem(tree)
                child.setText(0, display)
                child.setData(0, Qt.ItemDataRole.UserRole, key)
                try:
                    child.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            if not rows:
                placeholder = QTreeWidgetItem(tree)
                placeholder.setText(0, "(No entries)")
                placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsSelectable)

        except Exception as e:
            logger.exception("Error populating folder tree '%s'", category_name)

    def save_selected_focus_to_library(self):
        """Save currently selected focus's properties as a library entry"""
        selected = self.canvas.selectedItems()
        node = None
        for it in selected:
            if isinstance(it, FocusNode):
                node = it
                break
        if not node:
            QMessageBox.warning(self, "Warning", "No focus selected to save to library.")
            return
        focus = node.focus
        # Prompt for library key
        default_key = f"{focus.id}_{str(uuid.uuid4())[:8]}"
        key, ok = QInputDialog.getText(self, "Library Key", "Enter library key (unique):", QLineEdit.EchoMode.Normal, default_key)
        if not ok or not key.strip():
            return
        key = key.strip()
        if key in self.library:
            reply = QMessageBox.question(self, "Overwrite", f"Library key '{key}' already exists. Overwrite?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        entry = {
            "id": focus.id,
            "name": focus.name,
            "cost": focus.cost,
            "description": focus.description,
            "prerequisites": focus.prerequisites.copy(),
            "mutually_exclusive": focus.mutually_exclusive.copy(),
            "available": focus.available,
            "bypass": focus.bypass,
            "completion_reward": focus.completion_reward,
            "ai_will_do": focus.ai_will_do,
            "x": focus.x,
            "y": focus.y
        }
        self.library[key] = entry
        self.refresh_library_list()
        self.save_database()  # Auto-save database
        self.statusBar().showMessage(f"Saved focus '{focus.id}' to library as '{key}'")

    def delete_selected_library_entry(self):
        """Delete selected library entries (supports multi-selection)"""
        tree = self.get_active_folder_tree()
        selected_items = tree.selectedItems() if tree is not None else []
        if not selected_items:
            QMessageBox.warning(self, "Warning", "No library entries selected.")
            return

        # Filter out category nodes and collect valid keys
        valid_keys = []
        for item in selected_items:
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(key, str) and key.startswith("__category__::"):
                continue  # Skip category nodes
            if key and key in self.library:
                valid_keys.append(key)

        if not valid_keys:
            QMessageBox.warning(self, "Warning", "No valid library entries selected (categories cannot be deleted).")
            return

        # Confirm deletion
        if len(valid_keys) == 1:
            message = f"Delete library entry '{valid_keys[0]}'?"
        else:
            message = f"Delete {len(valid_keys)} library entries?"

        reply = QMessageBox.question(self, "Delete Library Entries", message,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            for key in valid_keys:
                if key in self.library:
                    del self.library[key]
            self.refresh_library_list()
            self.save_database()  # Auto-save database

    def apply_library_to_selected_focus(self):
        """Apply selected library entry to the currently selected focus node"""
        tree = self.get_active_folder_tree()
        sel = tree.currentItem() if tree is not None else None
        if not sel:
            QMessageBox.warning(self, "Warning", "No library entry selected.")
            return
        key = sel.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str) and key.startswith("__category__::"):
            QMessageBox.warning(self, "Warning", "Select an entry (not a category) to apply.")
            return
        entry = self.library.get(key)
        if not entry:
            QMessageBox.warning(self, "Warning", "Library entry not found.")
            return

        # Ensure entry is in dict format
        try:
            if isinstance(entry, dict):
                safe_entry = entry
            else:
                # Convert non-dict entries to dict format
                safe_entry = {"name": str(entry), "id": key}
        except Exception as e:
            QMessageBox.warning(self, "Warning", f"Invalid library entry format: {e}")
            return

        # Find selected focus node
        selected = self.canvas.selectedItems()
        node = None
        for it in selected:
            if isinstance(it, FocusNode):
                node = it
                break
        if not node:
            QMessageBox.warning(self, "Warning", "No focus node selected to apply library entry.")
            return
        # Apply fields to focus directly
        focus = node.focus
        focus.name = safe_entry.get("name", focus.name)
        focus.cost = safe_entry.get("cost", focus.cost)
        focus.description = safe_entry.get("description", focus.description)
        focus.prerequisites = safe_entry.get("prerequisites", focus.prerequisites)
        if isinstance(focus.prerequisites, str):
            focus.prerequisites = [p.strip() for p in focus.prerequisites.split(",") if p.strip()]
        elif not isinstance(focus.prerequisites, list):
            focus.prerequisites = []
        focus.mutually_exclusive = safe_entry.get("mutually_exclusive", focus.mutually_exclusive)
        if isinstance(focus.mutually_exclusive, str):
            focus.mutually_exclusive = [m.strip() for m in focus.mutually_exclusive.split(",") if m.strip()]
        elif not isinstance(focus.mutually_exclusive, list):
            focus.mutually_exclusive = []
        focus.available = safe_entry.get("available", focus.available)
        focus.bypass = safe_entry.get("bypass", focus.bypass)
        focus.completion_reward = safe_entry.get("completion_reward", focus.completion_reward)
        focus.ai_will_do = safe_entry.get("ai_will_do", focus.ai_will_do)
        # Update visuals
        if focus.id in self.canvas.nodes:
            self.canvas.nodes[focus.id].update()
        self.statusBar().showMessage(f"Applied library entry '{key}' to focus '{focus.id}'")

    def create_focus_from_library_selected(self):
        """Create new focuses on canvas from selected library entries (supports multi-selection)"""
        tree = self.get_active_folder_tree()
        selected_items = tree.selectedItems() if tree is not None else []
        if not selected_items:
            QMessageBox.warning(self, "Warning", "No library entries selected.")
            return

        # Filter out category nodes and collect valid entries
        valid_entries = []
        for item in selected_items:
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(key, str) and key.startswith("__category__::"):
                continue  # Skip category nodes
            entry = self.library.get(key)
            if entry:
                valid_entries.append((key, entry))

        if not valid_entries:
            QMessageBox.warning(self, "Warning", "No valid library entries selected (categories cannot be used to create focuses).")
            return

        created_count = 0
        for key, entry in valid_entries:
            try:
                # Ensure entry is in dict format
                if isinstance(entry, dict):
                    safe_entry = entry
                else:
                    # Convert non-dict entries to dict format
                    safe_entry = {"name": str(entry), "id": key}

                # Create a new focus with values from entry
                base_id = safe_entry.get("id") or f"focus_{str(uuid.uuid4())[:8]}"
                new_id = base_id
                counter = 1
                # Use the prefixed candidate when testing uniqueness so created ids
                # follow the project tag numbering (TAG_xxx_1, TAG_xxx_2 ...)
                new_pref = self._prefix_focus_id(new_id)
                while any(f.id == new_pref for f in self.focuses) or new_pref in getattr(self.canvas, 'nodes', {}):
                    new_id = f"{base_id}_{counter}"
                    counter += 1
                    new_pref = self._prefix_focus_id(new_id)

                # Handle prerequisites safely
                prereqs = safe_entry.get("prerequisites", [])
                if isinstance(prereqs, str):
                    prereqs = [p.strip() for p in prereqs.split(",") if p.strip()]
                elif not isinstance(prereqs, list):
                    prereqs = []

                # Handle mutually exclusive safely
                mutex = safe_entry.get("mutually_exclusive", [])
                if isinstance(mutex, str):
                    mutex = [m.strip() for m in mutex.split(",") if m.strip()]
                elif not isinstance(mutex, list):
                    mutex = []

                # Use the prefixed id as the stored id
                focus = Focus(
                    id=new_pref,
                    name=safe_entry.get("name", ""),
                    x=safe_entry.get("x", created_count * 2),  # Offset multiple focuses
                    y=safe_entry.get("y", 0),
                    cost=safe_entry.get("cost", 10),
                    description=safe_entry.get("description", ""),
                    prerequisites=prereqs,
                    mutually_exclusive=mutex,
                    available=safe_entry.get("available", ""),
                    bypass=safe_entry.get("bypass", ""),
                    completion_reward=safe_entry.get("completion_reward", ""),
                    ai_will_do=safe_entry.get("ai_will_do", 1)
                )
                self.focuses.append(focus)
                self.canvas.add_focus_node(focus)
                # Recreate connections for prerequisites (if those focuses already exist)
                for prereq in focus.prerequisites:
                    self.canvas.create_connection(prereq, focus.id)
                # Ensure mutual exclusivity reciprocity
                try:
                    self._sync_mutual_exclusive(focus.id)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                created_count += 1
            except Exception as e:
                logger.warning("Could not create focus from library entry '%s': %s", key, e)
                continue

        if created_count > 0:
            if created_count == 1:
                self.statusBar().showMessage(f"Created focus from library entry")
            else:
                self.statusBar().showMessage(f"Created {created_count} focuses from library entries")
            self.update_status()

    def on_library_item_double_clicked(self, item):
        """Double click -> create focus from library entry"""
        # If the double-clicked item is a category, toggle expand/collapse
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str) and key.startswith("__category__::"):
            item.setExpanded(not item.isExpanded())
            return
        self.create_focus_from_library_selected()

    def save_library_to_file(self):
        """Export library to JSON with folder structure"""
        # Sanitize tree_id for filename
        try:
            import re
            safe_tree_id = re.sub(r'[^A-Za-z0-9_\-]+', '_', str(self.tree_id or '')).strip('_') or 'library'
        except Exception:
            safe_tree_id = str(self.tree_id or 'library')
        filename, _ = QFileDialog.getSaveFileName(self, "Export Library", f"{safe_tree_id}_library.json", "JSON Files (*.json)")
        if not filename:
            return
        try:
            export_data = {
                "library": self.library,
                "folders": self.library_folders,
                "version": "1.0.9",
                "exported_date": time.time()
            }
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            QMessageBox.information(self, "Success", f"Library exported to {obfuscate_path(filename)}")
            self.statusBar().showMessage(f"Library exported to {obfuscate_path(filename)}")
        except Exception as e:
            show_error(self, "Error", "Failed to export library.", exc=e)

    def load_library_from_file(self):
        """Import library from JSON (with improved error handling and folder support)"""
        filename, _ = QFileDialog.getOpenFileName(self, "Import Library", "", "JSON Files (*.json)")
        if not filename:
            return
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                QMessageBox.warning(self, "Invalid", "Library file must be a JSON object mapping keys to entries.")
                return

            # Check if this is a new format with library and folders structure
            if "library" in data and "folders" in data:
                # New format with folder structure
                library_data = data["library"]
                folders_data = data.get("folders", {})

                # Import folder metadata
                for folder_key, folder_info in folders_data.items():
                    if folder_key not in self.library_folders:
                        self.library_folders[folder_key] = folder_info

                # Process library entries
                source_data = library_data
            else:
                # Legacy format - treat as direct library data
                source_data = data

            # Validate library entries with recursive expansion
            valid_entries = {}
            invalid_count = 0

            def _make_unique_key(base: str) -> str:
                candidate = base
                counter = 1
                while candidate in valid_entries or candidate in self.library:
                    candidate = f"{base}_{counter}"
                    counter += 1
                return candidate

            def _process_leaf(category_path: str, name: str):
                # Create an id-friendly base key from category and name
                # Use a short base key (category last part + index) to keep readability
                base = re.sub(r"[^0-9a-zA-Z_]+", "_", (category_path + "_" + name)[:80]).strip('_')
                if not base:
                    base = f"entry_{len(valid_entries)+1}"
                key = _make_unique_key(base)
                valid_entries[key] = {"id": key, "name": name, "category": category_path}

            def _recurse(prefix: str, obj):
                # prefix: e.g. 'political' or 'political/governance_structures'
                if isinstance(obj, dict):
                    for subk, subv in obj.items():
                        new_prefix = f"{prefix}/{subk}" if prefix else subk
                        _recurse(new_prefix, subv)
                elif isinstance(obj, (list, tuple)):
                    if len(obj) == 0:
                        return
                    # list of dicts
                    if all(isinstance(el, dict) for el in obj):
                        for idx, el in enumerate(obj):
                            # If dict contains 'name' or 'id', use as entry
                            if 'name' in el or 'id' in el:
                                subkey = el.get('id') or el.get('name') or f"{prefix}_{idx+1}"
                                key = _make_unique_key(str(subkey))
                                # preserve dict as entry but set category
                                new_entry = dict(el)
                                new_entry.setdefault('category', prefix)
                                new_entry['id'] = key
                                valid_entries[key] = new_entry
                            else:
                                # flatten nested dict into category and recurse
                                _recurse(prefix, el)
                    elif all(isinstance(el, str) for el in obj):
                        for name in obj:
                            _process_leaf(prefix, name)
                    else:
                        # mixed types: coerce to strings
                        for el in obj:
                            _process_leaf(prefix, str(el))
                else:
                    # Primitive type -> create a leaf
                    _process_leaf(prefix, str(obj))

            import re

            for key, entry in source_data.items():
                try:
                    # If the entry is a dict where values are lists/dicts, expand recursively
                    if isinstance(entry, dict) and any(isinstance(v, (dict, list, tuple)) for v in entry.values()):
                        _recurse(key, entry)
                    elif isinstance(entry, dict):
                        # treat as a single dict entry
                        # ensure entry has an id
                        the_key = entry.get('id') or key
                        the_key = _make_unique_key(str(the_key))
                        new_entry = dict(entry)
                        new_entry.setdefault('category', key)
                        new_entry['id'] = the_key
                        valid_entries[the_key] = new_entry
                    elif isinstance(entry, (list, tuple)):
                        # Empty list -> skip
                        if len(entry) == 0:
                            continue
                        _recurse(key, entry)
                    else:
                        # primitive -> single entry
                        the_key = _make_unique_key(str(key))
                        valid_entries[the_key] = {"id": the_key, "name": str(entry), "category": key}
                except Exception as e:
                    logger.warning("Skipping invalid library entry '%s': %s", key, e)
                    invalid_count += 1
                    continue

            if invalid_count > 0:
                QMessageBox.warning(self, "Import Warning", f"Skipped {invalid_count} invalid library entries.")

            if not valid_entries:
                QMessageBox.warning(self, "No Valid Entries", "No valid library entries found in file.")
                return

            # Merge into existing library, prompting on key collisions
            merge = QMessageBox.question(self, "Merge Libraries", "Merge imported library with current library?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if merge == QMessageBox.StandardButton.Yes:
                for key, entry in valid_entries.items():
                    if key in self.library:
                        # ask for overwrite for collisions
                        overwrite = QMessageBox.question(self, "Overwrite Entry", f"Key '{key}' exists. Overwrite?",
                                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                        if overwrite == QMessageBox.StandardButton.Yes:
                            self.library[key] = entry
                    else:
                        self.library[key] = entry
            else:
                # Replace
                self.library = valid_entries
            self.refresh_library_list()
            self.save_database()  # Auto-save database after import
            QMessageBox.information(self, "Success", f"Library imported from {obfuscate_path(filename)}")
            self.statusBar().showMessage(f"Library imported from {obfuscate_path(filename)}")
        except Exception as e:
            show_error(self, "Error", "Failed to import library.", exc=e)
            logger.exception("Library import error")
            import traceback
            traceback.print_exc()

    def show_library_context_menu(self, position):
        """Show context menu for library management.

        This resolves which widget (overview or a folder tree) the position refers to
        and builds an appropriate context menu for folder or entry actions.
        """
        tree = self.get_active_folder_tree()
        item = None
        widget_for_map = None
        if tree is not None:
            widget_for_map = tree
            item = tree.itemAt(position)
        else:
            widget_for_map = self.lib_overview_container

        menu = QMenu()
        if item:
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(key, str) and key.startswith("__category__::"):
                menu.addAction("Rename Folder", lambda: self.rename_library_folder(item))
                menu.addAction("Delete Folder", lambda: self.delete_library_folder(item))
                menu.addSeparator()
                menu.addAction("Create Subfolder", lambda: self.create_library_subfolder(item))
            else:
                menu.addAction("Move to Folder...", lambda: self.move_entries_to_folder())
                menu.addAction("Create Focus", self.create_focus_from_library_selected)
                menu.addAction("Apply to Selected Focus", self.apply_library_to_selected_focus)
                menu.addSeparator()
                menu.addAction("Delete Entry", self.delete_selected_library_entry)

        # Global actions
        if item:
            menu.addSeparator()
        menu.addAction("Create New Folder", self.create_new_library_folder)
        menu.addAction("Import Library...", self.load_library_from_file)
        menu.addAction("Export Library...", self.save_library_to_file)

        if widget_for_map is not None:
            menu.exec(widget_for_map.mapToGlobal(position))
        else:
            menu.exec(self.library_widget.mapToGlobal(position))

    def create_new_library_folder(self):
        """Create a new top-level folder"""
        name, ok = QInputDialog.getText(self, "New Folder", "Enter folder name:")
        if not ok or not name.strip():
            return

        folder_name = name.strip()
        # Ensure unique folder name
        counter = 1
        original_name = folder_name
        while any(key.startswith(f"__category__::{folder_name}") for key in self.library_folders):
            folder_name = f"{original_name}_{counter}"
            counter += 1

        folder_key = f"__category__::{folder_name}"
        self.library_folders[folder_key] = {
            "name": folder_name,
            "expanded": True,
            "created_date": time.time()
        }
        self.refresh_library_list()
        self.save_database()

    def rename_library_folder(self, item):
        """Rename a folder"""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(key, str) or not key.startswith("__category__::"):
            return

        old_name = key.replace("__category__::", "")
        new_name, ok = QInputDialog.getText(self, "Rename Folder", "Enter new folder name:",
                                           QLineEdit.EchoMode.Normal, old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return

        new_name = new_name.strip()
        new_key = f"__category__::{new_name}"

        # Update folder metadata
        if key in self.library_folders:
            self.library_folders[new_key] = self.library_folders[key]
            self.library_folders[new_key]["name"] = new_name
            del self.library_folders[key]

        # Update all entries in this category
        for entry_key, entry in self.library.items():
            if isinstance(entry, dict) and entry.get("category") == old_name:
                entry["category"] = new_name

        self.refresh_library_list()
        self.save_database()

    def delete_library_folder(self, item):
        """Delete a folder and optionally its contents"""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(key, str) or not key.startswith("__category__::"):
            return

        folder_name = key.replace("__category__::", "")

        # Count entries in this folder
        entries_in_folder = [k for k, v in self.library.items()
                           if isinstance(v, dict) and v.get("category") == folder_name]

        if entries_in_folder:
            reply = QMessageBox.question(
                self, "Delete Folder",
                f"Folder '{folder_name}' contains {len(entries_in_folder)} entries.\n"
                f"Delete folder and move entries to 'Ungrouped'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            # Move entries to ungrouped
            for entry_key in entries_in_folder:
                if entry_key in self.library:
                    self.library[entry_key]["category"] = "Ungrouped"

        # Delete folder metadata
        if key in self.library_folders:
            del self.library_folders[key]

        self.refresh_library_list()
        self.save_database()

    def move_entries_to_folder(self):
        """Move selected entries to a different folder"""
        tree = self.get_active_folder_tree()
        selected_items = tree.selectedItems() if tree is not None else []
        if not selected_items:
            QMessageBox.warning(self, "Warning", "No entries selected.")
            return

        # Get valid entry keys
        valid_keys = []
        for item in selected_items:
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(key, str) and key.startswith("__category__::"):
                continue  # Skip categories
            if key and key in self.library:
                valid_keys.append(key)

        if not valid_keys:
            QMessageBox.warning(self, "Warning", "No valid entries selected.")
            return

        # Get list of available folders
        folders = ["Ungrouped"]
        for folder_key, folder_data in self.library_folders.items():
            if folder_key.startswith("__category__::"):
                folders.append(folder_data.get("name", folder_key.replace("__category__::", "")))

        target_folder, ok = QInputDialog.getItem(
            self, "Move to Folder", "Select target folder:", folders, 0, False
        )
        if not ok:
            return

        # Move entries
        for key in valid_keys:
            if key in self.library and isinstance(self.library[key], dict):
                self.library[key]["category"] = target_folder

        self.refresh_library_list()
        self.save_database()
        self.statusBar().showMessage(f"Moved {len(valid_keys)} entries to '{target_folder}'")

    def create_library_subfolder(self, parent_item):
        """Create a subfolder under the selected folder"""
        # For now, treat as creating a new top-level folder
        # Could be enhanced later for true nesting
        self.create_new_library_folder()

    def on_folder_tree_double_clicked(self, item):
        """Handle double-click in a per-folder tree: create focus from the selected entry(s)."""
        # item is a QTreeWidgetItem representing an entry
        # We'll use the same creation logic as create_focus_from_library_selected
        self.create_focus_from_library_selected()

#endregion

# Main entry point
class _LoggerStream:
    """Small stream wrapper that redirects print/trace output into logging.

    We keep this tiny and explicit so startup diagnostics are easy to follow.
    """
    def __init__(self, log_fn):
        self._log_fn = log_fn

    def write(self, message):
        text = str(message).strip()
        if text:
            self._log_fn(text)

    def flush(self):
        return


def _setup_diagnostic_logging() -> Optional[str]:
    """Create a Logs folder and wire diagnostics to Logs/log.txt.

    This runs before QApplication startup so AppImage launch issues are
    captured even when the UI never appears.
    """
    try:
        appimage_path = os.environ.get('APPIMAGE', '').strip()
        appimage_logs = None
        if appimage_path:
            try:
                appimage_logs = Path(appimage_path).resolve().parent / 'Logs'
            except Exception:
                appimage_logs = None

        candidates = [
            # Prefer placing diagnostics beside the AppImage users launched.
            appimage_logs,
            Path.cwd() / 'Logs',
            Path.home() / '.focus_tool' / 'Logs',
            Path(tempfile.gettempdir()) / 'FocusTool' / 'Logs',
        ]

        log_dir = None
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                log_dir = candidate
                break
            except Exception:
                continue

        if log_dir is None:
            return None

        # Expose path for other modules so they can reuse one log location.
        os.environ['FOCUS_LOG_DIR'] = str(log_dir)
        log_file = log_dir / 'log.txt'

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s'))

        logging.basicConfig(level=logging.INFO, handlers=[file_handler], force=True)
        logger.info('Diagnostic logging initialized at %s', log_file)

        # Route print statements and uncaught exceptions to the diagnostics log.
        sys.stdout = _LoggerStream(logger.info)
        sys.stderr = _LoggerStream(logger.error)

        def _log_uncaught(exc_type, exc_val, exc_tb):
            logging.getLogger(__name__).critical('Uncaught exception', exc_info=(exc_type, exc_val, exc_tb))

        sys.excepthook = _log_uncaught
        return str(log_file)
    except Exception:
        return None


def main():
    _setup_diagnostic_logging()
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setApplicationName("HOI4 Focus GUI")
    app.setApplicationVersion("1.0.9")
    app.setOrganizationName("HOI4 Modding Community")
    handler = configure_error_handler(gui_parent=None, log_level="INFO")
    window = HOI4FocusTreeGenerator()
    try:
        handler.set_gui_parent(window)
    except Exception as e:
        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()