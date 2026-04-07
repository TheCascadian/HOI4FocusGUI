from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from error_handler import silent_operation

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor, QFontDatabase, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QColorDialog,
)

from _imports import (
    Event,
    Focus,
    format_project_file_size,
    obfuscate_text,
    obfuscate_user_in_path,
    set_widget_path_display,
    show_error,
)

logger = logging.getLogger(__name__)


class EditorDialogBase(QDialog):
    """Shared behaviour for editor dialogs.

    Keep this minimal. Add common helpers here (delete-on-close, scoped
    object-name setting, easy title setting) so future dialog panels can
    evolve without duplicating code.
    """
    def __init__(self, parent: Optional[QWidget] = None, title: Optional[str] = None, modal: bool = True):
        super().__init__(parent)
        with silent_operation("set_delete_on_close_attr"):
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        # --- Start of Patched Block ---
        # Only perform canvas-related updates if a canvas is actually present on the instance.
        canvas = getattr(self, 'canvas', None)
        if canvas is not None:
            try:
                # Consolidate all canvas operations into a single guarded block.
                if hasattr(canvas, 'refresh_connection_colors'):
                    canvas.refresh_connection_colors()
                if hasattr(canvas, 'schedule_frame_update'):
                    canvas.schedule_frame_update()
                if hasattr(canvas, 'update'):
                    canvas.update()

                for conn in list(getattr(canvas, 'connections', []) or []):
                    with silent_operation("set_connection_prereq_style"):
                        kind = getattr(conn, 'prereq_kind', None)
                        if kind and hasattr(conn, 'set_prereq_style'):
                            conn.set_prereq_style(kind)

                if hasattr(canvas, 'refresh_connection_colors'):
                    canvas.refresh_connection_colors()
                if hasattr(canvas, 'schedule_frame_update'):
                    canvas._frames_dirty = True
                    canvas.schedule_frame_update()
                if hasattr(canvas, 'update'):
                    canvas.update()
            except Exception:
                # Broad catch for safety if any canvas operation fails
                pass
        # --- End of Patched Block ---

        if title:
            with silent_operation("set_window_title"):
                self.setWindowTitle(title)
        if modal:
            with silent_operation("set_modal"):
                self.setModal(True)

    def set_styled_object_name(self, name: str):
        """Set object name for stylesheet scoping safely."""
        with silent_operation("set_object_name"):
            self.setObjectName(name)


__all__ = [
    "EditorDialogBase",
    "MultiAddDialog",
    "NodePaletteDialog",
    "FocusEditDialog",
    "EventEditDialog",
    "LayerManagerDialog",
    "ProjectsHomeDialog",
    "FindNotesDialog",
    "ProjectNoteSettingsDialog",
    "IconLibraryDialog",
    "SettingsDialog",
]


class MultiAddDialog(EditorDialogBase):
    """Dialog to create multiple focuses in a grid/row"""
    def __init__(self, parent=None, title: Optional[str] = "Multi-Add Focuses", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        # Ensure this dialog's widgets are not deleted when the dialog is closed/accepted.
        # Some callers call dialog.exec() and then read widget values afterward via get_values().
        # The base EditorDialogBase sets WA_DeleteOnClose which can cause C++ widgets to be
        # destroyed on accept(); disable that behaviour for this dialog instance.
        with silent_operation("disable_delete_on_close"):
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(360, 240)
        layout = QFormLayout()
        self.base_id = QLineEdit("focus_")
        self.base_name = QLineEdit("New Focus")
        self.count_spin = QSpinBox(); self.count_spin.setRange(1, 200); self.count_spin.setValue(3)
        self.direction_combo = QComboBox(); self.direction_combo.addItems(["Horizontal", "Vertical"])
        self.start_x = QSpinBox(); self.start_x.setRange(-100, 100); self.start_x.setValue(0)
        self.start_y = QSpinBox(); self.start_y.setRange(-100, 100); self.start_y.setValue(0)
        self.gap = QSpinBox(); self.gap.setRange(1, 10); self.gap.setValue(1)
        layout.addRow("Base ID:", self.base_id)
        layout.addRow("Base Name:", self.base_name)
        layout.addRow("Count:", self.count_spin)
        layout.addRow("Direction:", self.direction_combo)
        layout.addRow("Start X (grid):", self.start_x)
        layout.addRow("Start Y (grid):", self.start_y)
        layout.addRow("Gap (grid units):", self.gap)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)
        self.setLayout(layout)

    def get_values(self):
        return {
            'base_id': self.base_id.text().strip(),
            'base_name': self.base_name.text().strip(),
            'count': int(self.count_spin.value()),
            'direction': self.direction_combo.currentText(),
            'start_x': int(self.start_x.value()),
            'start_y': int(self.start_y.value()),
            'gap': int(self.gap.value()),
        }


class NodePaletteDialog(EditorDialogBase):
    """Grid-based palette to choose which node type to create."""
    def __init__(self, parent=None, title: Optional[str] = "New Node…", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.resize(420, 240)
        self.selection: Optional[str] = None
        self._build_ui()

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        lab = QLabel("Select a node type to create at the mouse position")
        lab.setWordWrap(True)
        v.addWidget(lab)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        # Define palette items: (id, label)
        items = [
            ("focus", "Focus"),
            ("event", "Event"),
            ("note", "Note"),
        ]
        for i, (sid, text) in enumerate(items):
            btn = QPushButton(text)
            btn.setMinimumSize(120, 56)
            btn.setIconSize(QSize(24, 24))
            btn.clicked.connect(lambda chk=False, s=sid: self._choose(s))
            r = i // 3
            c = i % 3
            grid.addWidget(btn, r, c)
        v.addLayout(grid)
        # bottom row
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        v.addLayout(row)

    def _choose(self, sid: str) -> None:
        self.selection = sid
        self.accept()


class FocusEditDialog(EditorDialogBase):
    """Enhanced dialog for editing focus properties"""
    def __init__(self, focus, parent=None, library: Optional[Dict[str, Dict[str, Any]]] = None, title: Optional[str] = None, modal: bool = True):
        # default title uses focus id when not provided
        resolved_title = title if title is not None else f"Edit Focus: {getattr(focus, 'id', '')}"
        super().__init__(parent, title=resolved_title, modal=modal)
        self.focus = focus
        self.library = library or {}
        self.resize(600, 700)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        # Create tabs for better organization
        tabs = QTabWidget()

        # Basic tab
        basic_tab = QWidget()
        basic_layout = QFormLayout()
        self.id_edit = QLineEdit(self.focus.id)
        self.name_edit = QLineEdit(self.focus.name)
        self.cost_spin = QSpinBox()
        self.cost_spin.setRange(1, 365)
        self.cost_spin.setValue(self.focus.cost)
        self.desc_edit = QTextEdit()
        with silent_operation("set_desc_tab_stop"):
            # Make tab spaces 2x smaller for compact display
            self.desc_edit.setTabStopDistance(self.desc_edit.fontMetrics().horizontalAdvance(' ') * 4)
        self.desc_edit.setPlainText(self.focus.description)
        self.desc_edit.setMaximumHeight(120)
        # Icon selector row
        # Use shortened display for long icon paths and keep full path in tooltip
        self.icon_label = QLabel()
        set_widget_path_display(self.icon_label, getattr(self.focus, 'icon', None), max_len=60)
        icon_btn = QPushButton("Choose Icon (.tga/.dds)...")
        def _choose_icon():
            fn, _ = QFileDialog.getOpenFileName(self, "Choose Icon", os.getcwd(), "Icons (*.tga *.dds)")
            if fn:
                # show a shortened label and keep full path in tooltip
                set_widget_path_display(self.icon_label, fn, max_len=120)
        remove_icon_btn = QPushButton("Remove Icon")
        def _remove_icon():
            self.icon_label.setText("(no icon)")
        from_lib_btn = QPushButton("From Library...")
        def _from_lib():
            with silent_operation("open_icon_library"):
                # parent is main editor; try to use its icon library
                editor = self.parent() if hasattr(self, 'parent') else None
                lib = getattr(editor, 'icon_library', {}) if editor else {}
                dlg = IconLibraryDialog(lib, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted and getattr(dlg, 'selected', None):
                    # icon library may return a short identifier; keep as-is but attach tooltip
                    sel = str(dlg.selected)
                    with silent_operation("set_icon_path_display"):
                        set_widget_path_display(self.icon_label, sel, max_len=60)
        from_lib_btn.clicked.connect(_from_lib)
        icon_btn.clicked.connect(_choose_icon)
        remove_icon_btn.clicked.connect(_remove_icon)

        # expose icon controls for update-helper wiring
        self.icon_btn = icon_btn
        self.remove_icon_btn = remove_icon_btn
        self.from_lib_btn = from_lib_btn

        icon_row = QHBoxLayout()
        icon_row.addWidget(self.icon_label)
        icon_row.addWidget(icon_btn)
        icon_row.addWidget(from_lib_btn)
        icon_row.addWidget(remove_icon_btn)
        basic_layout.addRow("Icon:", icon_row)
        basic_layout.addRow("ID:", self.id_edit)
        basic_layout.addRow("Name:", self.name_edit)
        basic_layout.addRow("Cost (Weeks):", self.cost_spin)
        basic_layout.addRow("Description:", self.desc_edit)
        basic_tab.setLayout(basic_layout)
        tabs.addTab(basic_tab, "Basic")

        # Advanced tab
        advanced_tab = QWidget()
        advanced_layout = QFormLayout()
        self.prereq_edit = QLineEdit(",".join(self.focus.prerequisites))
        self.mutex_edit = QLineEdit(",".join(self.focus.mutually_exclusive))
        # Checkbox to control grouped prerequisites output (either/or vs multiple blocks)
        self.prereq_grouped_chk = QCheckBox("Group prerequisites into a single block")
        with silent_operation("set_prereq_grouped_checked"):
            self.prereq_grouped_chk.setChecked(bool(getattr(self.focus, 'prerequisites_grouped', False)))
        self.available_edit = QTextEdit()
        with silent_operation("set_available_tab_stop"):
            self.available_edit.setTabStopDistance(self.available_edit.fontMetrics().horizontalAdvance(' ') * 4)
        self.available_edit.setPlainText(self.focus.available)
        self.available_edit.setMaximumHeight(80)
        self.bypass_edit = QTextEdit()
        with silent_operation("set_bypass_tab_stop"):
            self.bypass_edit.setTabStopDistance(self.bypass_edit.fontMetrics().horizontalAdvance(' ') * 4)
        self.bypass_edit.setPlainText(self.focus.bypass)
        self.bypass_edit.setMaximumHeight(80)
        self.ai_spin = QSpinBox()
        self.ai_spin.setRange(0, 100)
        self.ai_spin.setValue(self.focus.ai_will_do)
        advanced_layout.addRow("Prerequisites:", self.prereq_edit)
        advanced_layout.addRow("Grouping:", self.prereq_grouped_chk)
        # Prerequisite groups editor: list of groups, each with type (AND/OR) and comma-separated items
        groups_label = QLabel("Prerequisite groups (each group can be AND or OR and contains focus IDs):")
        advanced_layout.addRow(groups_label)
        self.prereq_groups_list = QListWidget()
        self.prereq_groups_list.setMaximumHeight(140)
        advanced_layout.addRow(self.prereq_groups_list)
        # add/remove buttons
        grp_btn_row = QWidget()
        grp_btn_layout = QHBoxLayout()
        grp_btn_row.setLayout(grp_btn_layout)
        self.add_group_btn = QPushButton("Add Group")
        self.remove_group_btn = QPushButton("Remove Selected")
        grp_btn_layout.addWidget(self.add_group_btn)
        grp_btn_layout.addWidget(self.remove_group_btn)
        advanced_layout.addRow(grp_btn_row)
        # Populate groups list from the focus model so existing groups are shown when dialog opens
        with silent_operation("populate_prereq_groups"):
            existing_groups = list(getattr(self.focus, 'prerequisites_groups', []) or [])
            if existing_groups:
                with silent_operation("clear_prereq_groups_list"):
                    self.prereq_groups_list.clear()
                for g in existing_groups:
                    with silent_operation("add_prereq_group_item"):
                        typ = (g.get('type') or 'AND') if isinstance(g, dict) else 'AND'
                        items = list(g.get('items', []) if isinstance(g, dict) else [])
                        itm = QListWidgetItem(f"{typ}: {','.join(items)}")
                        itm.setData(Qt.ItemDataRole.UserRole, {'type': typ, 'items': items})
                        with silent_operation("add_item_to_list"):
                            self.prereq_groups_list.addItem(itm)
        advanced_layout.addRow("Mutually Exclusive:", self.mutex_edit)
        advanced_layout.addRow("Available Condition:", self.available_edit)
        advanced_layout.addRow("Bypass Condition:", self.bypass_edit)

        # Branch gating: optional allow_branch condition
        self.allow_branch_chk = QCheckBox("Require another focus completion to unlock this branch")
        self.allow_branch_focus = QLineEdit()
        self.allow_branch_focus.setPlaceholderText("Focus ID")
        branch_row = QHBoxLayout()
        branch_row.addWidget(self.allow_branch_chk)
        branch_row.addWidget(QLabel("Focus ID:"))
        branch_row.addWidget(self.allow_branch_focus)
        branch_widget = QWidget()
        branch_widget.setLayout(branch_row)
        advanced_layout.addRow("Branch unlock condition:", branch_widget)

        # Completion reward helper: ensure mark_focus_tree_layout_dirty can be toggled easily.
        self.mark_dirty_chk = QCheckBox("Ensure completion_reward includes mark_focus_tree_layout_dirty = yes")
        advanced_layout.addRow(self.mark_dirty_chk)

        advanced_layout.addRow("AI Priority:", self.ai_spin)
        advanced_tab.setLayout(advanced_layout)
        tabs.addTab(advanced_tab, "Advanced")

        # Rewards tab
        rewards_tab = QWidget()
        # Use a vertical layout so the completion reward QTextEdit can expand to fill the tab
        rewards_layout = QVBoxLayout()
        self.reward_edit = QTextEdit()
        with silent_operation("set_reward_tab_stop"):
            self.reward_edit.setTabStopDistance(self.reward_edit.fontMetrics().horizontalAdvance(' ') * 4)
        # If this focus is linked to an Event via the canvas, display only a concise
        # reference in the focus rewards panel (per UX: only event reference/trigger
        # should appear here). Otherwise, show the stored completion_reward.
        with silent_operation("set_reward_edit_text"):
            displayed = None
            parent = self.parent() if hasattr(self, 'parent') else None
            if parent is not None and hasattr(parent, 'canvas'):
                for ef in getattr(parent.canvas, '_event_focus_links', []):
                    with silent_operation("check_event_focus_link"):
                        fnode = getattr(ef, 'focus_node', None)
                        evnode = getattr(ef, 'event_node', None)
                        if fnode and getattr(getattr(fnode, 'focus', None), 'id', None) == getattr(self.focus, 'id', None):
                            displayed = getattr(getattr(evnode, 'event', None), 'id', None)
                            if displayed:
                                break
            if displayed:
                self.reward_edit.setPlainText(f"country_event = {{ id = {displayed} }}")
            else:
                self.reward_edit.setPlainText(self.focus.completion_reward)
        # Allow the reward editor to expand and fill the tab
        with silent_operation("set_reward_edit_size_policy"):
            self.reward_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.reward_edit.setMinimumHeight(200)
        rewards_layout.addWidget(QLabel("Completion Reward:"))
        # Insert Effect button (opens effects browser and inserts snippet into reward editor)
        eff_row = QHBoxLayout()
        eff_row.addWidget(self.reward_edit)
        with silent_operation("setup_effects_inserter"):
            from _effects_inserter import EffectsInserterDialog

            insert_eff_btn = QPushButton("Insert Effect…")
            def _open_insert():
                with silent_operation("open_effects_inserter"):
                    dlg = EffectsInserterDialog(parent=self)
                    if dlg.exec() == QDialog.DialogCode.Accepted and getattr(dlg, 'selected_snippet', None):
                        cursor = self.reward_edit.textCursor()
                        cursor.insertText(dlg.selected_snippet)
                        self.reward_edit.setTextCursor(cursor)
                        self.reward_edit.setFocus()

            insert_eff_btn.clicked.connect(_open_insert)
            eff_row.addWidget(insert_eff_btn)

        rewards_layout.addLayout(eff_row)
        rewards_tab.setLayout(rewards_layout)
        tabs.addTab(rewards_tab, "Rewards")

        # Branch toggle wiring and initial states
        if getattr(self, 'allow_branch_chk', None):
            self.allow_branch_chk.stateChanged.connect(self._on_allow_branch_toggled)
        if getattr(self, 'allow_branch_focus', None):
            self.allow_branch_focus.textChanged.connect(self._on_allow_branch_text_changed)
        if getattr(self, 'mark_dirty_chk', None):
            self.mark_dirty_chk.stateChanged.connect(self._on_mark_dirty_toggled)

        self._load_allow_branch_state()
        self._refresh_mark_dirty_state()
        self._wire_live_update_handlers()

        # Relations tab: shows direct parents and children of this focus
        self.relations_tab = QWidget()
        rel_layout = QVBoxLayout()

        self.parents_label = QLabel("Parents (Prerequisites):")
        self.parents_list = QListWidget()
        self.parents_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        rel_layout.addWidget(self.parents_label)
        rel_layout.addWidget(self.parents_list)

        self.children_label = QLabel("Children (Depends on this):")
        self.children_list = QListWidget()
        self.children_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        rel_layout.addWidget(self.children_label)
        rel_layout.addWidget(self.children_list)

        self.relations_tab.setLayout(rel_layout)
        tabs.addTab(self.relations_tab, "Relations")

        # Export preview tab: show the HOI4 output for this single focus
        export_tab = QWidget()
        export_layout = QVBoxLayout()
        self.export_text = QTextEdit()
        with silent_operation("set_export_text_mono_font"):
            # monospace for code preview
            mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
            self.export_text.setFont(mono)
        with silent_operation("set_export_text_style"):
            # Render tabs visually as 4 spaces for tighter preview layout
            with silent_operation("set_export_tab_stop"):
                self.export_text.setTabStopDistance(self.export_text.fontMetrics().horizontalAdvance(' ') * 4)
            self.export_text.setReadOnly(True)
            self.export_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            self.export_text.setStyleSheet("background-color: #1e1e1e; color: #dcdcdc;")
        export_layout.addWidget(QLabel("Export preview (HOI4 code for this focus):"))
        export_layout.addWidget(self.export_text)
        export_tab.setLayout(export_layout)
        tabs.addTab(export_tab, "Export")

        layout.addWidget(tabs)

        # Library quick-apply selector (if library provided)
        if self.library is not None:
            lib_layout = QHBoxLayout()
            lib_layout.addWidget(QLabel("Quick apply library entry:"))
            self.lib_combo = QComboBox()
            self.lib_combo.setMinimumWidth(300)
            self.lib_combo.addItem("--- choose ---", None)
            for key, entry in self.library.items():
                with silent_operation("add_library_combo_item"):
                    if isinstance(entry, dict):
                        label = entry.get("name") or entry.get("id") or key
                    else:
                        label = str(entry) if entry else key
                    self.lib_combo.addItem(label, key)
            lib_apply_btn = QPushButton("Apply")
            lib_apply_btn.clicked.connect(self.apply_library_entry_to_fields)
            lib_layout.addWidget(self.lib_combo)
            lib_layout.addWidget(lib_apply_btn)
            layout.addLayout(lib_layout)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

        # Initial export preview refresh to avoid dependence on user toggle input
        try:
            if hasattr(self, '_update_export_preview'):
                self._update_export_preview()
        except Exception:
            pass

        # Populate relations lists and connect handlers
        with silent_operation("populate_relations"):
            self._populate_relations()
            self.parents_list.itemClicked.connect(self._on_relation_clicked)
            self.children_list.itemClicked.connect(self._on_relation_clicked)

    def _load_allow_branch_state(self):
        branch_text = str(getattr(self.focus, 'allow_branch', '') or '').strip()
        m = re.search(r'has_completed_focus\s*=\s*([A-Za-z0-9_\.-]+)', branch_text, re.IGNORECASE)
        if m:
            self.allow_branch_chk.setChecked(True)
            self.allow_branch_focus.setText(m.group(1).strip())
            self.allow_branch_focus.setEnabled(True)
        else:
            self.allow_branch_chk.setChecked(False)
            self.allow_branch_focus.setText('')
            self.allow_branch_focus.setEnabled(False)

    def _refresh_mark_dirty_state(self):
        text = str(self.reward_edit.toPlainText() or '')
        has_dirty = bool(re.search(r'^\s*mark_focus_tree_layout_dirty\s*=\s*yes\s*$', text, re.IGNORECASE | re.MULTILINE))
        self.mark_dirty_chk.setChecked(has_dirty)

    def _on_allow_branch_toggled(self, state):
        checked = bool(state)
        self.allow_branch_focus.setEnabled(checked)
        if not checked:
            self.allow_branch_focus.setText('')
        if hasattr(self, '_update_export_preview'):
            self._update_export_preview()

    def _on_allow_branch_text_changed(self, text):
        if self.allow_branch_chk.isChecked() and hasattr(self, '_update_export_preview'):
            self._update_export_preview()

    def _on_mark_dirty_toggled(self, state):
        text = str(self.reward_edit.toPlainText() or '')
        lines = [line for line in text.splitlines() if not re.match(r'^\s*mark_focus_tree_layout_dirty\s*=\s*yes\s*$', line, re.IGNORECASE)]
        if bool(state):
            if not any(re.match(r'^\s*mark_focus_tree_layout_dirty\s*=\s*yes\s*$', line, re.IGNORECASE) for line in lines):
                if lines and any(line.strip() for line in lines):
                    lines.append('mark_focus_tree_layout_dirty = yes')
                else:
                    lines = ['mark_focus_tree_layout_dirty = yes']
        self.reward_edit.setPlainText('\n'.join(lines).strip())
        if hasattr(self, '_update_export_preview'):
            self._update_export_preview()

    def _wire_live_update_handlers(self):
        # Live update export preview on any field change (including reward edits and icon changes)
        with silent_operation("setup_live_update_handlers"):
            # Keep export preview live
            self.id_edit.textChanged.connect(self._update_export_preview)
            self.name_edit.textChanged.connect(self._update_export_preview)
            self.cost_spin.valueChanged.connect(lambda *_: self._update_export_preview())
            self.desc_edit.textChanged.connect(self._update_export_preview)
            self.prereq_edit.textChanged.connect(self._update_export_preview)
            self.mutex_edit.textChanged.connect(self._update_export_preview)
            self.available_edit.textChanged.connect(self._update_export_preview)
            self.bypass_edit.textChanged.connect(self._update_export_preview)
            self.ai_spin.valueChanged.connect(lambda *_: self._update_export_preview())
            self.reward_edit.textChanged.connect(self._update_export_preview)

            # Also apply edits live to the focus model so visual changes appear instantly
            def _maybe_refresh_canvas():
                with silent_operation("maybe_refresh_canvas"):
                    parent = self.parent() if hasattr(self, 'parent') else None
                    canvas = getattr(parent, 'canvas', None) if parent is not None else None
                    if canvas and hasattr(canvas, 'schedule_frame_update'):
                        canvas.schedule_frame_update()

            def _apply_id(v):
                with silent_operation("apply_id"):
                    self.focus.id = v.strip()
                    _maybe_refresh_canvas()
            def _apply_name(v):
                with silent_operation("apply_name"):
                    self.focus.name = v
                    _maybe_refresh_canvas()
            def _apply_cost(v):
                with silent_operation("apply_cost"):
                    self.focus.cost = int(v)
                    _maybe_refresh_canvas()
            def _apply_desc():
                with silent_operation("apply_desc"):
                    self.focus.description = self.desc_edit.toPlainText()
                    _maybe_refresh_canvas()
            def _apply_prereqs(v):
                with silent_operation("apply_prereqs"):
                    self.focus.prerequisites = [p.strip() for p in v.split(',') if p.strip()]
                    _maybe_refresh_canvas()
            def _apply_prereq_grouped(v=None):
                with silent_operation("apply_prereq_grouped"):
                    self.focus.prerequisites_grouped = bool(self.prereq_grouped_chk.isChecked())
                    _maybe_refresh_canvas()
            def _apply_mutex(v):
                with silent_operation("apply_mutex"):
                    self.focus.mutually_exclusive = [m.strip() for m in v.split(',') if m.strip()]
                    _maybe_refresh_canvas()
            def _apply_available():
                with silent_operation("apply_available"):
                    self.focus.available = self.available_edit.toPlainText()
                    _maybe_refresh_canvas()
            def _apply_bypass():
                with silent_operation("apply_bypass"):
                    self.focus.bypass = self.bypass_edit.toPlainText()
                    _maybe_refresh_canvas()
            def _apply_ai(v):
                with silent_operation("apply_ai"):
                    self.focus.ai_will_do = int(v)
                    _maybe_refresh_canvas()
            # wire live-update handlers with focus model updates
            self.id_edit.textChanged.connect(_apply_id)
            self.name_edit.textChanged.connect(_apply_name)
            self.cost_spin.valueChanged.connect(_apply_cost)
            self.desc_edit.textChanged.connect(lambda *_: _apply_desc())
            self.prereq_edit.textChanged.connect(_apply_prereqs)
            self.prereq_grouped_chk.stateChanged.connect(lambda *_: _apply_prereq_grouped())
            self.mutex_edit.textChanged.connect(_apply_mutex)
            self.available_edit.textChanged.connect(lambda *_: _apply_available())
            self.bypass_edit.textChanged.connect(lambda *_: _apply_bypass())
            self.ai_spin.valueChanged.connect(_apply_ai)
            # completion_reward updates now only through mark_dirty checkbox and final accept
            # the text field still updates preview but does not trigger live render sync.
            # self.reward_edit.textChanged.connect(lambda *_: _apply_reward())

            # Also refresh when icon changes via any of the buttons
            def _refresh_after_icon_change():
                with silent_operation("refresh_after_icon_change"):
                    self._update_export_preview()
            self.icon_btn.clicked.connect(_refresh_after_icon_change)
            self.remove_icon_btn.clicked.connect(_refresh_after_icon_change)
            self.from_lib_btn.clicked.connect(_refresh_after_icon_change)

            # Initial render
            self._update_export_preview()

        # Wire group controls (Add/Remove/Edit) if they exist
        with silent_operation("wire_group_controls"):
            if hasattr(self, 'add_group_btn'):
                self.add_group_btn.clicked.connect(lambda *_: self._on_add_group())
            if hasattr(self, 'remove_group_btn'):
                self.remove_group_btn.clicked.connect(lambda *_: self._on_remove_group())
            if hasattr(self, 'prereq_groups_list'):
                self.prereq_groups_list.itemDoubleClicked.connect(lambda itm: self._on_edit_group_item(itm))

    def apply_library_entry_to_fields(self):
        key = self.lib_combo.currentData()
        if not key:
            return
        entry = self.library.get(key)
        if not entry:
            return

        # Ensure entry is in dict format
        try:
            if isinstance(entry, dict):
                safe_entry = entry
            else:
                # Convert non-dict entries to dict format
                safe_entry = {"name": str(entry), "id": key}
        except Exception:
            return

        # Apply relevant fields to the dialog (not auto-saving to focus until accept)
        self.id_edit.setText(safe_entry.get("id", self.id_edit.text()))
        self.name_edit.setText(safe_entry.get("name", self.name_edit.text()))
        self.cost_spin.setValue(safe_entry.get("cost", self.cost_spin.value()))
        self.desc_edit.setPlainText(safe_entry.get("description", self.desc_edit.toPlainText()))

        # Handle prerequisites safely
        prereqs = safe_entry.get("prerequisites", [])
        if isinstance(prereqs, list):
            self.prereq_edit.setText(",".join(prereqs))
        else:
            self.prereq_edit.setText(str(prereqs) if prereqs else "")
        # Apply grouping flag if provided by the library entry
        with silent_operation("apply_prereq_grouped_from_library"):
            if 'prerequisites_grouped' in safe_entry:
                self.prereq_grouped_chk.setChecked(bool(safe_entry.get('prerequisites_grouped')))
        # Load prerequisite groups if provided
        with silent_operation("load_prereq_groups_from_library"):
            groups = safe_entry.get('prerequisites_groups', None)
            if groups is not None and hasattr(self, 'prereq_groups_list'):
                with silent_operation("populate_prereq_groups_from_library"):
                    self.prereq_groups_list.clear()
                    for g in groups:
                        typ = g.get('type', 'AND')
                        items = g.get('items', []) or []
                        itm = QListWidgetItem(f"{typ}: {','.join(items)}")
                        itm.setData(Qt.ItemDataRole.UserRole, {'type': typ, 'items': items})
                        self.prereq_groups_list.addItem(itm)

        # Handle mutually exclusive safely
        mutex = safe_entry.get("mutually_exclusive", [])
        if isinstance(mutex, list):
            self.mutex_edit.setText(",".join(mutex))
        else:
            self.mutex_edit.setText(str(mutex) if mutex else "")

        self.available_edit.setPlainText(safe_entry.get("available", self.available_edit.toPlainText()))
        self.bypass_edit.setPlainText(safe_entry.get("bypass", self.bypass_edit.toPlainText()))
        # If focus is linked to an event, show only the concise event reference
        with silent_operation("check_event_focus_links_for_reward"):
            displayed = None
            parent = self.parent() if hasattr(self, 'parent') else None
            if parent is not None and hasattr(parent, 'canvas'):
                for ef in getattr(parent.canvas, '_event_focus_links', []):
                    with silent_operation("check_single_event_focus_link"):
                        fnode = getattr(ef, 'focus_node', None)
                        evnode = getattr(ef, 'event_node', None)
                        if fnode and getattr(getattr(fnode, 'focus', None), 'id', None) == safe_entry.get('id', self.id_edit.text()):
                            displayed = getattr(getattr(evnode, 'event', None), 'id', None)
                            if displayed:
                                break
            if displayed:
                self.reward_edit.setPlainText(f"country_event = {{ id = {displayed} }}")
            else:
                self.reward_edit.setPlainText(safe_entry.get("completion_reward", self.reward_edit.toPlainText()))
        self.ai_spin.setValue(safe_entry.get("ai_will_do", self.ai_spin.value()))
        with silent_operation("refresh_export_preview_after_library"):
            # refresh export preview after applying library entry
            if hasattr(self, '_update_export_preview'):
                with silent_operation("call_update_export_preview"):
                    self._update_export_preview()

    def accept(self):
        # Update focus with new values
        self.focus.id = self.id_edit.text().strip()
        self.focus.name = self.name_edit.text().strip()
        self.focus.cost = self.cost_spin.value()
        self.focus.description = self.desc_edit.toPlainText()
        # store icon if set
        icon_text = self.icon_label.text().strip()
        self.focus.icon = None if icon_text in ("(no icon)", "") else icon_text
        self.focus.prerequisites = [p.strip() for p in self.prereq_edit.text().split(",") if p.strip()]
        self.focus.mutually_exclusive = [m.strip() for m in self.mutex_edit.text().split(",") if m.strip()]
        # store grouped flag
        with silent_operation("store_prerequisites_grouped"):
            self.focus.prerequisites_grouped = bool(self.prereq_grouped_chk.isChecked())
        # store groups
        with silent_operation("store_prereq_groups"):
            groups = []
            if hasattr(self, 'prereq_groups_list'):
                for i in range(self.prereq_groups_list.count()):
                    item = self.prereq_groups_list.item(i)
                    g = item.data(Qt.ItemDataRole.UserRole)
                    if not g:
                        txt = item.text()
                        if ':' in txt:
                            typ, rest = txt.split(':', 1)
                            items = [s.strip() for s in rest.split(',') if s.strip()]
                            g = {'type': typ.strip(), 'items': items}
                        else:
                            continue
                    groups.append({'type': g.get('type', 'AND'), 'items': list(g.get('items', []))})
            self.focus.prerequisites_groups = groups
        self.focus.available = self.available_edit.toPlainText()
        self.focus.bypass = self.bypass_edit.toPlainText()

        # Branch allow_branch toggle
        branch_val = ''
        if getattr(self, 'allow_branch_chk', None) and self.allow_branch_chk.isChecked():
            candidate = str(getattr(self, 'allow_branch_focus', None).text().strip() if getattr(self, 'allow_branch_focus', None) else '').strip()
            if candidate:
                branch_val = f"has_completed_focus = {candidate}"
        self.focus.allow_branch = branch_val

        # Completion reward dirty-mark toggle
        reward_val = self.reward_edit.toPlainText()
        if getattr(self, 'mark_dirty_chk', None) and self.mark_dirty_chk.isChecked():
            if not re.search(r"^\s*mark_focus_tree_layout_dirty\s*=\s*yes\s*$", reward_val, re.IGNORECASE | re.MULTILINE):
                if reward_val.strip():
                    reward_val = reward_val.rstrip() + "\nmark_focus_tree_layout_dirty = yes"
                else:
                    reward_val = "mark_focus_tree_layout_dirty = yes"
        else:
            reward_val = "\n".join([line for line in reward_val.splitlines() if not re.match(r"^\s*mark_focus_tree_layout_dirty\s*=\s*yes\s*$", line, re.IGNORECASE)])
        self.focus.completion_reward = reward_val.strip()

        self.focus.ai_will_do = self.ai_spin.value()
        # Ensure mutual exclusivity is symmetric in the main model
        with silent_operation("sync_mutual_exclusive"):
            parent = self.parent() if hasattr(self, 'parent') else None
            if parent is not None and hasattr(parent, '_sync_mutual_exclusive'):
                with silent_operation("call_sync_mutual_exclusive"):
                    parent._sync_mutual_exclusive(self.focus.id)
        # Sync visual connections on the canvas to reflect any manual prerequisite edits
        with silent_operation("sync_canvas_connections"):
            parent = self.parent() if hasattr(self, 'parent') else None
            if parent is not None and hasattr(parent, 'canvas'):
                canvas = parent.canvas
                # Ensure connections exist for each declared prerequisite (parent_id -> this_focus_id)
                my_id = getattr(self.focus, 'id', None)
                # Start with explicit prerequisites
                explicit_parents = list(getattr(self.focus, 'prerequisites', []) or [])
                # Include any parents declared via prerequisite groups and remember their group type for styling
                parent_kind = {}
                groups = list(getattr(self.focus, 'prerequisites_groups', []) or [])
                for g in groups:
                    with silent_operation("process_prereq_group"):
                        typ = (g.get('type') or 'AND') if isinstance(g, dict) else 'AND'
                        items = list(g.get('items', []) if isinstance(g, dict) else [])
                        for pid in items:
                            if pid and pid not in parent_kind:
                                parent_kind[pid] = typ

                # desired_parents is the union of explicit parents and group items
                desired_parents = list(dict.fromkeys(explicit_parents + list(parent_kind.keys())))

                # Create missing connections and update styling for group-declared parents
                for pid in desired_parents:
                    with silent_operation("create_or_update_connection"):
                        if not pid or pid not in getattr(canvas, 'nodes', {}):
                            continue
                        # check if connection exists
                        exists = None
                        for conn in list(getattr(canvas, 'connections', [])):
                            with silent_operation("check_connection"):
                                if not (hasattr(conn, 'start_node') and hasattr(conn, 'end_node')):
                                    continue
                                s = getattr(getattr(conn, 'start_node', None), 'focus', None)
                                e = getattr(getattr(conn, 'end_node', None), 'focus', None)
                                if s is None or e is None:
                                    continue
                                if getattr(s, 'id', None) == pid and getattr(e, 'id', None) == my_id:
                                    exists = conn
                                    break
                        if exists is None:
                            with silent_operation("create_new_connection"):
                                line = canvas.create_connection(pid, my_id)
                                # if this parent came from a group, attach kind/style
                                kind = parent_kind.get(pid)
                                if kind and hasattr(line, 'set_prereq_style'):
                                    with silent_operation("set_new_conn_prereq_style"):
                                        line.prereq_kind = kind
                                        line.set_prereq_style(kind)
                        else:
                            # update existing connection's style if group declares a kind
                            with silent_operation("update_existing_connection_style"):
                                kind = parent_kind.get(pid)
                                if kind:
                                    exists.prereq_kind = kind
                                    if hasattr(exists, 'set_prereq_style'):
                                        with silent_operation("set_existing_conn_prereq_style"):
                                            exists.set_prereq_style(kind)

                # Remove stale connections that no longer correspond to any declared prerequisite
                with silent_operation("remove_stale_connections"):
                    for conn in list(getattr(canvas, 'connections', [])):
                        with silent_operation("check_stale_connection"):
                            if not (hasattr(conn, 'start_node') and hasattr(conn, 'end_node')):
                                continue
                            s = getattr(getattr(conn, 'start_node', None), 'focus', None)
                            e = getattr(getattr(conn, 'end_node', None), 'focus', None)
                            if s is None or e is None:
                                continue
                            # Only consider connections that end at this focus
                            if getattr(e, 'id', None) != my_id:
                                continue
                            if getattr(s, 'id', None) not in desired_parents:
                                with silent_operation("remove_connection"):
                                    canvas.remove_connection(conn)

                # Refresh visuals (mutex connectors and line colors)
                with silent_operation("refresh_mutex_connectors"):
                    canvas.refresh_mutex_connectors()
                with silent_operation("refresh_connection_colors"):
                    canvas.refresh_connection_colors()
                with silent_operation("update_canvas"):
                    canvas.update()
                with silent_operation("schedule_frame_update"):
                    # schedule frame/layout update if available
                    canvas._frames_dirty = True
                    canvas.schedule_frame_update()
        super().accept()

    def _populate_relations(self):
        # Clear lists
        self.parents_list.clear()
        self.children_list.clear()
        # Parents: explicit prerequisites
        parents = getattr(self.focus, 'prerequisites', []) or []
        for p in parents:
            itm = QListWidgetItem(str(p))
            itm.setData(Qt.ItemDataRole.UserRole, p)
            self.parents_list.addItem(itm)

        # Children: find focuses in editor that list this focus as prereq
        editor = self.parent()
        candidates = []
        if editor is None:
            return
        if hasattr(editor, 'focuses') and isinstance(editor.focuses, list):
            candidates = editor.focuses
        elif hasattr(editor, 'canvas') and hasattr(editor.canvas, 'nodes'):
            candidates = [n.focus for n in editor.canvas.nodes.values() if hasattr(n, 'focus')]

        my_id = getattr(self.focus, 'id', None)
        for f in candidates:
            if getattr(f, 'id', None) == my_id:
                continue
            prereqs = getattr(f, 'prerequisites', []) or []
            if my_id in prereqs:
                label = getattr(f, 'title', None) or getattr(f, 'name', None) or str(getattr(f, 'id', ''))
                itm = QListWidgetItem(label)
                itm.setData(Qt.ItemDataRole.UserRole, getattr(f, 'id', None))
                self.children_list.addItem(itm)

    # --- Prerequisite groups editor handlers ---
    def _on_add_group(self):
        with silent_operation("add_prereq_group"):
            # Ask for group type first
            typ, ok = QInputDialog.getItem(self, 'Add Prerequisite Group', 'Type:', ['AND', 'OR'], 0, False)
            if not ok:
                return
            items_txt, ok2 = QInputDialog.getText(self, 'Add Prerequisite Group', 'Items (comma-separated IDs):')
            if not ok2:
                return
            typ = typ or 'AND'
            items = [s.strip() for s in str(items_txt or '').split(',') if s.strip()]
            g = {'type': typ, 'items': items}
            itm = QListWidgetItem(f"{typ}: {','.join(items)}")
            itm.setData(Qt.ItemDataRole.UserRole, g)
            with silent_operation("add_group_to_list"):
                self.prereq_groups_list.addItem(itm)
            with silent_operation("update_export_preview_after_add_group"):
                self._update_export_preview()

    def _on_remove_group(self):
        with silent_operation("remove_prereq_group"):
            sels = list(self.prereq_groups_list.selectedItems()) if hasattr(self, 'prereq_groups_list') else []
            for s in sels:
                with silent_operation("remove_single_group"):
                    row = self.prereq_groups_list.row(s)
                    self.prereq_groups_list.takeItem(row)
            with silent_operation("update_export_preview_after_remove_group"):
                self._update_export_preview()

    def _on_edit_group_item(self, item: QListWidgetItem):
        with silent_operation("edit_prereq_group_item"):
            g = item.data(Qt.ItemDataRole.UserRole) or {}
            dlg = QDialog(self)
            dlg.setWindowTitle('Edit Prerequisite Group')
            layout = QFormLayout(dlg)
            type_cb = QComboBox(); type_cb.addItems(['AND', 'OR'])
            with silent_operation("set_type_cb_current_text"):
                type_cb.setCurrentText(g.get('type', 'AND'))
            items_edit = QLineEdit(','.join(g.get('items', []) if g else []))
            layout.addRow('Type:', type_cb)
            layout.addRow('Items:', items_edit)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg)
            layout.addWidget(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                typ = type_cb.currentText() or 'AND'
                items = [s.strip() for s in items_edit.text().split(',') if s.strip()]
                new_g = {'type': typ, 'items': items}
                item.setText(f"{typ}: {','.join(items)}")
                item.setData(Qt.ItemDataRole.UserRole, new_g)
                with silent_operation("update_export_preview_after_edit_group"):
                    self._update_export_preview()

    def _on_relation_clicked(self, item: QListWidgetItem):
        target = item.data(Qt.ItemDataRole.UserRole)
        if not target:
            return
        with silent_operation("handle_relation_clicked"):
            editor = self.parent()
            if not editor:
                return
            canvas = getattr(editor, 'canvas', None)
            view = getattr(editor, 'view', None)
            if canvas and hasattr(canvas, 'nodes'):
                node = canvas.nodes.get(target)
                if node:
                    # Select the node and center the view
                    node.setSelected(True)
                    if view and hasattr(view, 'centerOn'):
                        view.centerOn(node)
                if hasattr(canvas, 'highlight_lineage'):
                    canvas.highlight_lineage(target)

    def _format_focus_export(self) -> str:
        """Return a HOI4-formatted single focus block using the main window's exact formatter."""
        with silent_operation("format_focus_export"):
            # Build a temporary Focus reflecting current editor field values
            branch_val = ''
            if getattr(self, 'allow_branch_chk', None) and self.allow_branch_chk.isChecked():
                branch_target = getattr(self, 'allow_branch_focus', None)
                if branch_target is not None:
                    t = str(branch_target.text().strip())
                    if t:
                        branch_val = f"has_completed_focus = {t}"

            tmp = Focus(
                id=self.id_edit.text().strip() or getattr(self.focus, 'id', ''),
                name=self.name_edit.text().strip(),
                x=int(getattr(self.focus, 'x', 0) or 0),
                y=int(getattr(self.focus, 'y', 0) or 0),
                cost=int(self.cost_spin.value()),
                description=self.desc_edit.toPlainText(),
                prerequisites=[p.strip() for p in self.prereq_edit.text().split(',') if p.strip()],
                mutually_exclusive=[m.strip() for m in self.mutex_edit.text().split(',') if m.strip()],
                available=self.available_edit.toPlainText(),
                bypass=self.bypass_edit.toPlainText(),
                completion_reward=self.reward_edit.toPlainText(),
                ai_will_do=int(self.ai_spin.value()),
                allow_branch=branch_val,
                network_id=getattr(self.focus, 'network_id', None),
                icon=(None if (self.icon_label.text().strip() in ("(no icon)", "")) else self.icon_label.text().strip()),
            )
            # if prereq groups UI present, attach to tmp so parent.format_focus_block can use it
            with silent_operation("attach_prereq_groups_to_tmp"):
                if hasattr(self, 'prereq_groups_list'):
                    groups = []
                    for i in range(self.prereq_groups_list.count()):
                        item = self.prereq_groups_list.item(i)
                        g = item.data(Qt.ItemDataRole.UserRole) or {}
                        if not g:
                            txt = item.text()
                            if ':' in txt:
                                typ, rest = txt.split(':', 1)
                                g = {'type': typ.strip(), 'items': [s.strip() for s in rest.split(',') if s.strip()]}
                            else:
                                continue
                        groups.append({'type': g.get('type', 'AND'), 'items': list(g.get('items', []))})
                    tmp.prerequisites_groups = groups
            parent = self.parent() if hasattr(self, 'parent') else None
            if parent is not None and hasattr(parent, 'format_focus_block'):
                return str(parent.format_focus_block(tmp) or '')
        # Fallback to empty string on any error
        return ""

    def _update_export_preview(self) -> None:
        with silent_operation("update_export_preview"):
            text = self._format_focus_export()
            self.export_text.setPlainText(text)


class EventEditDialog(EditorDialogBase):
    """Dialog for editing Event properties and script blocks."""
    def __init__(self, event: Event, parent=None, title: Optional[str] = None, modal: bool = True):
        resolved_title = title if title is not None else f"Edit Event: {getattr(event, 'id', '')}"
        super().__init__(parent, title=resolved_title, modal=modal)
        self.event = event
        self.resize(560, 560)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        tabs = QTabWidget()
        # Basic
        basic_tab = QWidget(); basic_form = QFormLayout(basic_tab)
        self.id_edit = QLineEdit(self.event.id)
        self.title_edit = QLineEdit(self.event.title)
        self.desc_edit = QTextEdit(); self.desc_edit.setPlainText(self.event.description); self.desc_edit.setMaximumHeight(140)
        with silent_operation("set_desc_edit_tab_stop"):
            self.desc_edit.setTabStopDistance(self.desc_edit.fontMetrics().horizontalAdvance(' ') * 4)
        # Ensure text is visible in dark themes: set a neutral dark background and light text
        with silent_operation("set_desc_edit_style"):
            self.desc_edit.setStyleSheet("background-color: #2b2b2b; color: #eaeaea;")
            # prefer plain text to avoid unexpected rich text rendering
            with silent_operation("set_desc_accept_rich_text"):
                self.desc_edit.setAcceptRichText(False)
        basic_form.addRow("ID:", self.id_edit)
        basic_form.addRow("Title:", self.title_edit)
        basic_form.addRow("Description:", self.desc_edit)
        tabs.addTab(basic_tab, "Basic")
        # Script
        script_tab = QWidget(); script_form = QFormLayout(script_tab)
        # Provide placeholder if empty to help new users and ensure edit box isn't blank
        trig_text = self.event.trigger if (isinstance(self.event.trigger, str) and self.event.trigger.strip()) else f"trigger = {{\n\t# conditions for {self.event.id}\n}}"
        self.trigger_edit = QTextEdit()
        self.trigger_edit.setPlainText(trig_text)
        self.trigger_edit.setMaximumHeight(160)
        with silent_operation("set_trigger_edit_tab_stop"):
            self.trigger_edit.setTabStopDistance(self.trigger_edit.fontMetrics().horizontalAdvance(' ') * 4)
        # Make sure text is readable in dark themes
        with silent_operation("set_trigger_edit_style"):
            self.trigger_edit.setStyleSheet("background-color: #2b2b2b; color: #eaeaea;")
            with silent_operation("set_trigger_accept_rich_text"):
                self.trigger_edit.setAcceptRichText(False)

        opts_text = self.event.options_block if (isinstance(self.event.options_block, str) and self.event.options_block.strip()) else (f"option = {{\n\tname = {self.event.id}.a\n\t# add effects here\n}}\n\n# Add additional options as needed\n")
        self.options_edit = QTextEdit()
        self.options_edit.setPlainText(opts_text)
        self.options_edit.setMaximumHeight(220)
        with silent_operation("set_options_edit_tab_stop"):
            self.options_edit.setTabStopDistance(self.options_edit.fontMetrics().horizontalAdvance(' ') * 4)
        with silent_operation("set_options_edit_style"):
            self.options_edit.setStyleSheet("background-color: #2b2b2b; color: #eaeaea;")
            with silent_operation("set_options_accept_rich_text"):
                self.options_edit.setAcceptRichText(False)

        # Option localisations UI: allow adding multiple option keys with
        # localized text. These are stored on the Event as
        # event.option_loc_values (dict) and event.option_keys (ordered list).
        # Only the first key will be used by the renderer for display.
        try:
            loc_widget = QWidget()
            loc_layout = QVBoxLayout(loc_widget)
            loc_layout.setContentsMargins(0, 0, 0, 0)
            loc_layout.setSpacing(6)

            loc_header = QLabel("Option localisations (first key is used for rendering):")
            loc_layout.addWidget(loc_header)

            # Container for rows
            self._option_rows_container = QWidget()
            self._option_rows_layout = QVBoxLayout(self._option_rows_container)
            self._option_rows_layout.setContentsMargins(0, 0, 0, 0)
            self._option_rows_layout.setSpacing(4)
            loc_layout.addWidget(self._option_rows_container)

            # Add button
            add_btn = QPushButton("Add option")
            def _on_add_clicked():
                try:
                    # compute next key: prefer letters a..z then numbers
                    pid = getattr(self.event, 'id', '') or 'event.1'
                    # collect existing keys
                    existing = set()
                    try:
                        for i in range(self._option_rows_layout.count()):
                            w = self._option_rows_layout.itemAt(i).widget()
                            if not w:
                                continue
                            edits = w.findChildren(QLineEdit)
                            if edits:
                                k = edits[0].text().strip()
                                if k:
                                    existing.add(k)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        evk = getattr(self.event, 'option_keys', None)
                        if isinstance(evk, (list, tuple)):
                            for k in evk:
                                if k:
                                    existing.add(k)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    i = 0
                    next_key = None
                    while True:
                        if i < 26:
                            suffix = chr(ord('a') + i)
                        else:
                            suffix = str(i + 1)
                        candidate = f"{pid}.{suffix}"
                        if candidate not in existing:
                            next_key = candidate
                            break
                        i += 1
                    if not next_key:
                        next_key = f"{pid}.a"
                    self._add_option_row(next_key, '')
                    self._gather_option_localisations()
                    # Ensure the raw options_block includes a corresponding option entry
                    try:
                        import re as _re
                        opts_txt = str(self.options_edit.toPlainText() or '')
                        # look for a name = <next_key> occurrence to avoid duplicate insertion
                        pattern = _re.compile(r"name\s*=\s*" + _re.escape(next_key) + r"\b")
                        if not pattern.search(opts_txt):
                            # append a minimal option block for the new key
                            block = (f"option = {{\n\tname = {next_key}\n\t# add effects here\n}}\n")
                            if opts_txt.strip():
                                new_opts = opts_txt.rstrip() + "\n\n" + block
                            else:
                                new_opts = block
                            # programmatically update the options editor (this will trigger live-apply)
                            try:
                                self.options_edit.setPlainText(new_opts)
                            except Exception:
                                try:
                                    self.options_edit.insertPlainText(new_opts)
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            # explicitly apply options to ensure event data synced
                            try:
                                _apply_options()
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            add_btn.clicked.connect(_on_add_clicked)
            loc_layout.addWidget(add_btn)

            # small helper methods for managing rows
            def _make_row(key_text, loc_text):
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(6)
                key_edit = QLineEdit(str(key_text))
                key_edit.setPlaceholderText('option key (e.g. event.1.a)')
                key_edit.setMinimumWidth(160)
                loc_edit = QLineEdit(str(loc_text))
                loc_edit.setPlaceholderText('localized text shown for this option')
                remove = QPushButton('Remove')
                remove.setMaximumWidth(80)
                h.addWidget(QLabel('Key:'))
                h.addWidget(key_edit)
                h.addWidget(QLabel('Text:'))
                h.addWidget(loc_edit)
                h.addWidget(remove)

                def _on_change(*_):
                    try:
                        self._gather_option_localisations()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                key_edit.textChanged.connect(_on_change)
                loc_edit.textChanged.connect(_on_change)

                def _on_remove():
                    try:
                        self._option_rows_layout.removeWidget(row)
                        row.deleteLater()
                        self._gather_option_localisations()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                remove.clicked.connect(_on_remove)
                return row, key_edit, loc_edit

            # store shallow refs for helpers
            self._option_row_factory = _make_row

            def _add_option_row_impl(k, t):
                row, key_edit, loc_edit = _make_row(k, t)
                self._option_rows_layout.addWidget(row)
                try:
                    if key_edit and key_edit.text().strip():
                        # auto-filled keys should not be editable
                        key_edit.setReadOnly(True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return row, key_edit, loc_edit

            self._add_option_row = _add_option_row_impl
            self._gather_option_localisations = lambda : None

            # actual implementation of gather that builds the dict/list
            def _gather_impl():
                try:
                    vals = {}
                    keys = []
                    # iterate children widgets in the layout
                    for i in range(self._option_rows_layout.count()):
                        w = self._option_rows_layout.itemAt(i).widget()
                        if not w:
                            continue
                        # assume structure from _make_row: children are QLineEdits in positions
                        edits = w.findChildren(QLineEdit)
                        if not edits:
                            continue
                        key = edits[0].text().strip()
                        text = edits[1].text().strip() if len(edits) > 1 else ''
                        if key:
                            vals[key] = text
                            keys.append(key)
                    # store on event
                    try:
                        self.event.option_loc_values = vals
                        self.event.option_keys = keys
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    # update export preview and canvas
                    try:
                        self._update_export_preview()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        parent = self.parent() if hasattr(self, 'parent') else None
                        canvas = getattr(parent, 'canvas', None) if parent is not None else None
                        if canvas and hasattr(canvas, 'schedule_frame_update'):
                            canvas.schedule_frame_update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # bind the real gather implementation
            self._gather_option_localisations = _gather_impl

            # load any existing values from the event
            try:
                existing = getattr(self.event, 'option_loc_values', None) or {}
                existing_keys = getattr(self.event, 'option_keys', None) or list(existing.keys())
                if isinstance(existing, dict) and existing_keys:
                    for k in existing_keys:
                        self._add_option_row(k, existing.get(k, ''))
                else:
                    # start with one blank row for convenience
                    # but prefill the key if we can compute a sensible default
                    try:
                        pid = getattr(self.event, 'id', '') or 'event.1'
                        first_key = f"{pid}.a"
                        # if the event already has explicit keys, honor them
                        if not getattr(self.event, 'option_keys', None):
                            # initialize event keys so editor shows non-editable key
                            try:
                                self.event.option_keys = [first_key]
                                self.event.option_loc_values = {first_key: ''}
                                self._add_option_row(first_key, '')
                            except Exception:
                                self._add_option_row('', '')
                        else:
                            self._add_option_row('', '')
                    except Exception:
                        self._add_option_row('', '')
                # ensure the gatherer runs to sync initial state
                self._gather_option_localisations()
            except Exception:
                try:
                    self._add_option_row('', '')
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # place the localisation widget below the raw options editor
            script_form.addRow("Trigger:", self.trigger_edit)
            script_form.addRow("Options block:", self.options_edit)
            script_form.addRow(loc_widget)
        except Exception:
            # fall back to original layout if anything fails
            script_form.addRow("Trigger:", self.trigger_edit)
            script_form.addRow("Options block:", self.options_edit)
        tabs.addTab(script_tab, "Script")
        # Export preview tab for Event: inserted before adding tabs to layout
        export_tab = QWidget()
        export_layout = QVBoxLayout()
        self.export_text = QTextEdit()
        with silent_operation("set_export_text_mono_font_event"):
            mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
            self.export_text.setFont(mono)
        with silent_operation("set_export_text_style_event"):
            # Render tabs visually as 4 spaces for tighter preview layout
            with silent_operation("set_export_tab_stop_event"):
                self.export_text.setTabStopDistance(self.export_text.fontMetrics().horizontalAdvance(' ') * 4)
            self.export_text.setReadOnly(True)
            self.export_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            self.export_text.setStyleSheet("background-color: #1e1e1e; color: #dcdcdc;")
        export_layout.addWidget(QLabel("Export preview (HOI4 event code for this event):"))
        export_layout.addWidget(self.export_text)
        export_tab.setLayout(export_layout)
        tabs.addTab(export_tab, "Export")
        layout.addWidget(tabs)
        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.setLayout(layout)
        # connect live-update signals for export preview and apply changes live
        with silent_operation("connect_event_live_update_signals"):
            self.id_edit.textChanged.connect(self._update_export_preview)
            self.title_edit.textChanged.connect(self._update_export_preview)
            self.desc_edit.textChanged.connect(self._update_export_preview)
            self.trigger_edit.textChanged.connect(self._update_export_preview)
            self.options_edit.textChanged.connect(self._update_export_preview)

            def _maybe_refresh_canvas():
                with silent_operation("maybe_refresh_canvas_event"):
                    parent = self.parent() if hasattr(self, 'parent') else None
                    canvas = getattr(parent, 'canvas', None) if parent is not None else None
                    if canvas and hasattr(canvas, 'schedule_frame_update'):
                        canvas.schedule_frame_update()

            def _apply_id(v):
                with silent_operation("apply_event_id"):
                    self.event.id = v.strip()
                    _maybe_refresh_canvas()
            def _apply_title(v):
                with silent_operation("apply_event_title"):
                    self.event.title = v
                    _maybe_refresh_canvas()
            def _apply_desc():
                with silent_operation("apply_event_desc"):
                    self.event.description = self.desc_edit.toPlainText()
                    _maybe_refresh_canvas()
            def _apply_trigger():
                with silent_operation("apply_event_trigger"):
                    self.event.trigger = self.trigger_edit.toPlainText()
                    _maybe_refresh_canvas()
            def _apply_options():
                with silent_operation("apply_event_options"):
                    self.event.options_block = self.options_edit.toPlainText()
                    _maybe_refresh_canvas()

            self.id_edit.textChanged.connect(_apply_id)
            self.title_edit.textChanged.connect(_apply_title)
            self.desc_edit.textChanged.connect(lambda *_: _apply_desc())
            self.trigger_edit.textChanged.connect(lambda *_: _apply_trigger())
            self.options_edit.textChanged.connect(lambda *_: _apply_options())

            # initial render
            self._update_export_preview()

    def accept(self):
        with silent_operation("accept_event_dialog"):
            old_id = self.event.id
            self.event.id = self.id_edit.text().strip() or old_id
            self.event.title = self.title_edit.text().strip()
            self.event.description = self.desc_edit.toPlainText()
            self.event.trigger = self.trigger_edit.toPlainText()
            self.event.options_block = self.options_edit.toPlainText()
        super().accept()

    def _format_event_export(self) -> str:
        with silent_operation("format_event_export"):
            # Build a temporary Event from current fields
            tmp = Event(
                id=self.id_edit.text().strip() or getattr(self.event, 'id', ''),
                title=self.title_edit.text().strip(),
                description=self.desc_edit.toPlainText(),
                x=int(getattr(self.event, 'x', 0) or 0),
                y=int(getattr(self.event, 'y', 0) or 0),
                trigger=self.trigger_edit.toPlainText(),
                options_block=self.options_edit.toPlainText(),
            )
            parent = self.parent() if hasattr(self, 'parent') else None
            if parent is not None and hasattr(parent, 'format_event_block'):
                return str(parent.format_event_block(tmp) or '')
        return ""

    def _update_export_preview(self) -> None:
        with silent_operation("update_export_preview_event"):
            txt = self._format_event_export()
            self.export_text.setPlainText(txt)


class LayerManagerDialog(EditorDialogBase):
    """Dialog to list subtree layers and allow the user to toggle visibility and pick colors."""
    def __init__(self, canvas: 'FocusTreeCanvas', parent=None, title: Optional[str] = "Layer Manager", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.canvas = canvas
        self.resize(360, 420)
        self._widgets: Dict[int, QCheckBox] = {}
        self._color_buttons: Dict[int, QPushButton] = {}
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        info = QLabel("Toggle visibility and pick colors for subtree layers:")
        layout.addWidget(info)
        self.list_widget = QWidget()
        list_layout = QFormLayout()
        # detect existing layers from canvas.layer_colors and canvas._layer_frame_items
        layers = sorted(set(list(self.canvas.layer_colors.keys()) + list(self.canvas._layer_frame_items.keys())))
        if not layers:
            layers = [0]
        for lid in layers:
            chk = QCheckBox(f"Layer {lid}")
            chk.setChecked(self.canvas.layer_visibility.get(lid, True))
            btn = QPushButton()
            col = self.canvas.layer_colors.get(lid, QColor(200, 200, 200))
            pix = QPixmap(24, 16)
            pix.fill(col)
            def make_pick(l, b):
                def _pick():
                    current = self.canvas.layer_colors.get(l, QColor(200, 200, 200))
                    new = QColorDialog.getColor(current, self, f"Pick color for layer {l}")
                    if new.isValid():
                        self.canvas.layer_colors[l] = new
                        p = QPixmap(24, 16)
                        p.fill(new)
                        b.setIcon(QIcon(p))
                return _pick
            btn.clicked.connect(make_pick(lid, btn))
            list_layout.addRow(chk, btn)
            self._widgets[lid] = chk
            self._color_buttons[lid] = btn
        self.list_widget.setLayout(list_layout)
        layout.addWidget(self.list_widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def get_visibilities(self) -> Dict[int, bool]:
        return {lid: chk.isChecked() for lid, chk in self._widgets.items()}


class ProjectsHomeDialog(EditorDialogBase):
    """A simple homepage to scan a folder for project JSONs and load/create projects quickly."""
    def __init__(self, start_dir: Optional[str] = None, parent=None, title: Optional[str] = "Projects Home", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.resize(700, 420)
        self.start_dir = start_dir or os.getcwd()
        self.selected_path: Optional[str] = None
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        top = QHBoxLayout()
        # show obfuscated path in the UI but keep real path in self.start_dir
        with silent_operation("projects_home_import_lru_cache"):
            from functools import lru_cache
        self.dir_edit = QLineEdit(obfuscate_user_in_path(self.start_dir) if 'obfuscate_user_in_path' in globals() else self.start_dir)
        self.dir_edit.setReadOnly(True)
        browse = QPushButton("Browse…")
        def _browse():
            # open native dialog at real start path
            real_start = self.start_dir or os.getcwd()
            fn = QFileDialog.getExistingDirectory(self, "Choose Projects Folder", real_start)
            if fn:
                self.start_dir = fn
                # display obfuscated/shortened and set tooltip to full path
                if 'obfuscate_user_in_path' in globals():
                    self.dir_edit.setText(obfuscate_user_in_path(fn))
                    with silent_operation("projects_home_set_tooltip"):
                        self.dir_edit.setToolTip(fn)
                else:
                    set_widget_path_display(self.dir_edit, fn)
                self.refresh()
        browse.clicked.connect(_browse)
        top.addWidget(QLabel("Folder:"))
        top.addWidget(self.dir_edit)
        top.addWidget(browse)
        layout.addLayout(top)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Double-click a project to open it immediately
        with silent_operation("projects_home_double_click_connect"):
            self.list.itemDoubleClicked.connect(lambda item: (setattr(self, 'selected_path', item.data(Qt.ItemDataRole.UserRole)) or self.accept()))
        layout.addWidget(self.list)

        # Per-project preference: when set, new/loaded projects will prefer app-wide settings
        with silent_operation("projects_home_setup_preference_checkbox"):
            self.prefer_app_settings_chk = QCheckBox('Prefer app-wide settings over per-project (for new projects)')
            # default unchecked; the main window may override when opening a project
            self.prefer_app_settings_chk.setChecked(False)
            layout.addWidget(self.prefer_app_settings_chk)
            # Persist the preference when toggled: if a project is selected, write it into
            # that project's JSON under settings.preferences.prefer_app_settings. If no
            # project is selected, update the main window settings (if available).
            def _on_prefer_toggled(checked: bool):
                with silent_operation("projects_home_prefer_toggled"):
                    # If an item is selected, prefer to persist to that project's file
                    it = self.list.currentItem()
                    if it:
                        p = it.data(Qt.ItemDataRole.UserRole)
                        if p and os.path.isfile(p):
                            pdata = None
                            with silent_operation("projects_home_load_project_json"):
                                with open(p, 'r', encoding='utf-8') as f:
                                    pdata = json.load(f)
                            if isinstance(pdata, dict):
                                with silent_operation("projects_home_persist_preference"):
                                    settings = pdata.get('settings') or {}
                                    prefs = settings.get('preferences') or {}
                                    prefs['prefer_app_settings'] = bool(checked)
                                    settings['preferences'] = prefs
                                    pdata['settings'] = settings
                                    # write back
                                    with open(p, 'w', encoding='utf-8') as f:
                                        json.dump(pdata, f, indent=2)
                                    # update label to reflect persistence (size may change)
                                    st = 0
                                    with silent_operation("projects_home_get_file_size"):
                                        st = os.path.getsize(p)
                                    # Recompute counts for label: total nodes and per-type breakdown
                                    _focus_ct = len(pdata.get('focuses', []) or []) if isinstance(pdata, dict) else 0
                                    _event_ct = len(pdata.get('events', []) or []) if isinstance(pdata, dict) else 0
                                    _note_ct = len(pdata.get('notes', []) or []) if isinstance(pdata, dict) else 0
                                    _total_ct = _focus_ct + _event_ct + _note_ct
                                    label = (
                                        f"{os.path.basename(p)}  —  total {_total_ct}  "
                                        f"(notes: {_note_ct}, focuses: {_focus_ct}, events: {_event_ct})  —  {format_project_file_size(st, show_bytes=False)}"
                                    )
                                    it.setText(label)
                                    # If parent exists and is main window, update its in-memory flag
                                    with silent_operation("projects_home_update_parent_flag"):
                                        mw = self.parent()
                                        if getattr(mw, 'prefer_app_settings', None) is not None:
                                            mw.prefer_app_settings = bool(checked)
                    else:
                        # No project selected: update main window settings so subsequent saves persist
                        with silent_operation("projects_home_update_main_window_settings"):
                            mw = self.parent()
                            if mw is not None and hasattr(mw, 'prefer_app_settings'):
                                mw.prefer_app_settings = bool(checked)
                                with silent_operation("projects_home_save_settings"):
                                    if hasattr(mw, 'save_settings'):
                                        mw.save_settings()
            with silent_operation("projects_home_connect_toggled"):
                self.prefer_app_settings_chk.toggled.connect(_on_prefer_toggled)
            # When the selection in the projects list changes, update the checkbox to
            # reflect the stored preference for that project (or the main window's
            # in-memory value when no project selected). Block signals while updating
            # so we don't accidentally persist again.
            def _on_selection_changed():
                with silent_operation("projects_home_selection_changed"):
                    it = self.list.currentItem()
                    # Block signals to avoid re-triggering the persistence handler
                    with silent_operation("projects_home_block_signals"):
                        self.prefer_app_settings_chk.blockSignals(True)
                    if not it:
                        # No project selected: reflect main window's in-memory value
                        with silent_operation("projects_home_reflect_main_window_value"):
                            mw = self.parent()
                            if mw is not None and hasattr(mw, 'prefer_app_settings'):
                                self.prefer_app_settings_chk.setChecked(bool(getattr(mw, 'prefer_app_settings', False)))
                            else:
                                self.prefer_app_settings_chk.setChecked(False)
                    else:
                        p = it.data(Qt.ItemDataRole.UserRole)
                        val = False
                        if p and os.path.isfile(p):
                            with silent_operation("projects_home_read_project_preference"):
                                with open(p, 'r', encoding='utf-8') as f:
                                    pdata = json.load(f)
                                if isinstance(pdata, dict):
                                    settings = pdata.get('settings') or {}
                                    prefs = settings.get('preferences') or {}
                                    val = bool(prefs.get('prefer_app_settings', False))
                        with silent_operation("projects_home_set_checkbox_value"):
                            self.prefer_app_settings_chk.setChecked(bool(val))
                    with silent_operation("projects_home_unblock_signals"):
                        self.prefer_app_settings_chk.blockSignals(False)

            with silent_operation("projects_home_connect_selection_changed"):
                # connect selection changes to updater
                self.list.itemSelectionChanged.connect(_on_selection_changed)
                # initialize checkbox state now that list is populated
                _on_selection_changed()

        btns = QHBoxLayout()
        load_btn = QPushButton("Load")
        new_btn = QPushButton("New Project")
        cancel_btn = QPushButton("Cancel")
        btns.addWidget(load_btn)
        btns.addWidget(new_btn)
        btns.addStretch()
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        load_btn.clicked.connect(self._do_load)
        new_btn.clicked.connect(self._do_new)
        cancel_btn.clicked.connect(self.reject)

        self.setLayout(layout)
        self.refresh()

    def refresh(self):
        folder = self.start_dir or os.getcwd()
        self.list.clear()
        with silent_operation("projects_home_refresh_list"):
            for fn in sorted(os.listdir(folder)):
                if not fn.lower().endswith('.json'):
                    continue
                path = os.path.join(folder, fn)
                # Only list files that look like valid project JSONs (contain a 'focuses' list)
                is_project = False
                focus_count = 0
                event_count = 0
                note_count = 0
                with silent_operation("projects_home_check_project_file"):
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get('focuses', None), list):
                        is_project = True
                        focus_count = len(data.get('focuses') or [])
                        event_count = len(data.get('events') or []) if isinstance(data.get('events', None), list) else 0
                        note_count = len(data.get('notes') or []) if isinstance(data.get('notes', None), list) else 0
                if not is_project:
                    continue
                st = 0
                with silent_operation("projects_home_get_size"):
                    st = os.path.getsize(path)
                total = focus_count + event_count + note_count
                label = (
                    f"{fn}  —  total {total}  "
                    f"(notes: {note_count}, focuses: {focus_count}, events: {event_count})  —  {format_project_file_size(st, show_bytes=True)}"
                )
                itm = QListWidgetItem(label)
                itm.setData(Qt.ItemDataRole.UserRole, path)
                self.list.addItem(itm)

    def _do_load(self):
        it = self.list.currentItem()
        if not it:
            QMessageBox.information(self, "Projects Home", "Select a project file to load.")
            return
        self.selected_path = it.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _do_new(self):
        name, ok = QInputDialog.getText(self, "New Project", "Enter new project filename (without extension):")
        if not ok or not name.strip():
            return
        # Prompt for a country TAG: must be three letters and not the literal 'TAG'
        tag_attempts = 0
        tag = None
        while tag_attempts < 3:
            tag_attempts += 1
            t, tok = QInputDialog.getText(self, "Country TAG", "Enter 3-letter country TAG (not 'TAG'):")
            if not tok:
                # user cancelled
                return
            t = (t or '').strip().upper()
            # Accept 3 letters (alpha) or allow other reasonable formats except 'TAG'
            if t and t != 'TAG' and len(t) == 3 and t.isalpha():
                tag = t
                break
            QMessageBox.warning(self, "Invalid TAG", "Please enter a valid 3-letter country TAG (not 'TAG').")
        if not tag:
            QMessageBox.warning(self, "New Project", "Failed to set a valid country TAG. Aborting.")
            return
        # Use the same folder resolution logic as the main app
        folder = self.start_dir
        if not folder:
            # Fall back to cwd only as last resort
            folder = os.getcwd()
        fn = os.path.join(folder, f"{name.strip()}.json")
        # create an empty base project
        # Persist project preference for app-settings precedence if the checkbox exists
        prefer = False
        try:
            prefer = bool(getattr(self, 'prefer_app_settings_chk', None) and self.prefer_app_settings_chk.isChecked())
        except Exception:
            prefer = False
        base = {
            'version': getattr(self.parent(), 'app_version', '1.0.9') if self.parent() else '1.0.9',
            'tree_id': name.strip(),
            'country_tag': tag,
            'focuses': [],
            'library': {},
            'settings': {
                'preferences': {
                    'prefer_app_settings': bool(prefer)
                }
            }
        }
        try:
            with open(fn, 'w', encoding='utf-8') as f:
                json.dump(base, f, indent=2)
            self.selected_path = fn
            self.accept()
        except Exception as e:
            show_error(self, "New Project", "Failed to create project.", exc=e)

    pass


class FindNotesDialog(EditorDialogBase):
    """Simple dialog to list notes and jump to them."""
    def __init__(self, notes: List['NoteNode'], parent=None, title: Optional[str] = "Find Notes", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.resize(420, 360)
        self._notes = notes


class ProjectNoteSettingsDialog(EditorDialogBase):
    """Dialog to configure project-level default Note settings (fonts/colors/connections)."""
    def __init__(self, canvas: 'FocusTreeCanvas', parent=None, title: Optional[str] = "Project Note Settings", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self._canvas = canvas
        d = getattr(canvas, 'note_defaults', {}) or {}
        lay = QFormLayout(self)
        # Font sizes
        self.title_size = QSpinBox(); self.title_size.setRange(6, 48); self.title_size.setValue(int(d.get('title_size', 11)))
        self.body_size = QSpinBox(); self.body_size.setRange(6, 48); self.body_size.setValue(int(d.get('body_size', 11)))
        lay.addRow("Title font size:", self.title_size)
        lay.addRow("Body font size:", self.body_size)
        # Colors
        def _color_row(label: str, key: str):
            h = QHBoxLayout()
            edit = QLineEdit(str(d.get(key, '')))
            btn = QPushButton("Pick…")
            def _pick():
                try:
                    cur = QColor(edit.text()) if edit.text().strip() else QColor("#000000")
                except Exception:
                    cur = QColor("#000000")
                col = QColorDialog.getColor(cur, self, label)
                if col.isValid():
                    edit.setText(col.name(QColor.NameFormat.HexArgb))
            btn.clicked.connect(_pick)
            h.addWidget(edit); h.addWidget(btn)
            lay.addRow(label, h)
            return edit
        self.title_color = _color_row("Title color:", 'title_color')
        self.text_color = _color_row("Text color:", 'text_color')
        self.bg_color = _color_row("Note background:", 'bg_color')
        self.conn_color = _color_row("Connection color:", 'connection_color')
        # Connection width
        self.conn_width = QSpinBox(); self.conn_width.setRange(1, 12); self.conn_width.setValue(int(d.get('connection_width', 2)))
        lay.addRow("Connection width:", self.conn_width)
        # Buttons
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        def _acc():
            nd = getattr(self._canvas, 'note_defaults', {})
            nd['title_size'] = int(self.title_size.value())
            nd['body_size'] = int(self.body_size.value())
            if self.title_color.text().strip():
                nd['title_color'] = self.title_color.text().strip()
            if self.text_color.text().strip():
                nd['text_color'] = self.text_color.text().strip()
            if self.bg_color.text().strip():
                nd['bg_color'] = self.bg_color.text().strip()
            if self.conn_color.text().strip():
                nd['connection_color'] = self.conn_color.text().strip()
            nd['connection_width'] = int(self.conn_width.value())
            self.accept()
        bb.accepted.connect(_acc)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)


class IconLibraryDialog(EditorDialogBase):
    """Dialog to manage and choose icon identifiers for focuses (.tga/.dds only)."""
    def __init__(self, icons: Dict[str, str], parent=None, title: Optional[str] = "Icon Library", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.resize(520, 420)
        self.icons = icons  # name -> path (or identifier)
        self.selected: Optional[str] = None
        self._setup_ui()

    def _setup_ui(self):
        v = QVBoxLayout()
        h = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search icons…")
        self.search.textChanged.connect(self._refresh)
        add_btn = QPushButton("Add…")
        rem_btn = QPushButton("Remove")
        h.addWidget(self.search); h.addWidget(add_btn); h.addWidget(rem_btn)
        v.addLayout(h)
        self.list = QListWidget(); self.list.itemDoubleClicked.connect(self._accept)
        v.addWidget(self.list)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)
        add_btn.clicked.connect(self._add)
        rem_btn.clicked.connect(self._remove)
        self.setLayout(v)
        # Populate the list initially
        with silent_operation("icon_library_initial_refresh"):
            self._refresh()

    def accept(self) -> None:
        """Apply advanced settings (file logging) when the dialog is accepted."""
        # Apply file logging choice if the dialog was created with a canvas that has logging controls
        with silent_operation("icon_library_apply_file_logging"):
            parent = getattr(self.canvas, 'parent', None)
            if parent is not None and hasattr(self, 'file_logging_chk') and hasattr(self, 'log_path_edit'):
                to_file = bool(self.file_logging_chk.isChecked())
                # update path if user changed it
                lp = str(self.log_path_edit.text()).strip() or None
                if lp:
                    parent.log_file_path = lp
                # ensure main has attributes
                parent.logging_to_file = to_file
                parent.setup_file_logging(to_file, lp)

        # Persist icon library changes to any top-level window that owns the same dict
        with silent_operation("icon_library_persist_to_top_level"):
            from PyQt6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                with silent_operation("icon_library_save_widget_database"):
                    if getattr(w, 'icon_library', None) is self.icons and hasattr(w, 'save_database'):
                        w.save_database()
        # Also attempt direct parent save for reliability
        with silent_operation("icon_library_persist_to_parent"):
            p = self.parent()
            if p is not None and hasattr(p, 'save_database'):
                p.save_database()

        # Finalize the dialog acceptance
        return super().accept()

    def _valid_icon_file(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        return ext in ('.tga', '.dds')

    def _add(self):
        fns, _ = QFileDialog.getOpenFileNames(self, "Add Icons (.tga/.dds)", os.getcwd(), "Icons (*.tga *.dds)")
        if not fns:
            return
        # Limit batch size to avoid accidentally adding huge numbers of icons
        MAX_BATCH = 64
        if len(fns) > MAX_BATCH:
            with silent_operation("icon_library_batch_limit_message"):
                QMessageBox.information(self, "Icon Library", f"Selected {len(fns)} icons. Importing the first {MAX_BATCH}.")
            fns = fns[:MAX_BATCH]
        added = 0
        for fn in fns:
            with silent_operation("icon_library_add_single_icon"):
                if not fn:
                    continue
                if not self._valid_icon_file(fn):
                    continue
                name = os.path.splitext(os.path.basename(fn))[0]
                # ensure unique key
                key = name
                i = 1
                while key in self.icons:
                    key = f"{name}_{i}"
                    i += 1
                self.icons[key] = fn
                added += 1
        if added == 0:
            QMessageBox.information(self, "Icon Library", "No valid icons were added.")
            return
        # refresh UI once for the batch
        self._refresh()
        # Persist changes to any top-level owner that shares this dict (do once)
        with silent_operation("icon_library_add_persist_to_top_level"):
            from PyQt6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                with silent_operation("icon_library_add_save_widget_database"):
                    if getattr(w, 'icon_library', None) is self.icons and hasattr(w, 'save_database'):
                        w.save_database()
        # Also attempt direct parent save for reliability
        with silent_operation("icon_library_add_persist_to_parent"):
            p = self.parent()
            if p is not None and hasattr(p, 'save_database'):
                p.save_database()

    def _remove(self):
        it = self.list.currentItem()
        if not it:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        if key in self.icons:
            del self.icons[key]
        self._refresh()
        # Persist changes to any top-level owner that shares this dict
        with silent_operation("icon_library_remove_persist_to_top_level"):
            from PyQt6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                with silent_operation("icon_library_remove_save_widget_database"):
                    if getattr(w, 'icon_library', None) is self.icons and hasattr(w, 'save_database'):
                        w.save_database()

    def _refresh(self):
        q = (self.search.text() or '').strip().lower()
        self.list.clear()
        for k, v in sorted(self.icons.items()):
            if q and q not in k.lower() and q not in str(v).lower():
                continue
            item = QListWidgetItem(f"{k} — {os.path.basename(str(v))}")
            item.setData(Qt.ItemDataRole.UserRole, k)
            self.list.addItem(item)

    def _accept(self):
        it = self.list.currentItem()
        if not it:
            QMessageBox.information(self, "Icon Library", "Select an icon.")
            return
        self.selected = it.data(Qt.ItemDataRole.UserRole)
        self.accept()


class SettingsDialog(EditorDialogBase):
    """Full settings panel with live updates to the canvas."""
    def __init__(self, canvas: FocusTreeCanvas, parent=None, title: Optional[str] = "Settings", modal: bool = True):
        super().__init__(parent, title=title, modal=modal)
        self.canvas = canvas
        # Mark dialog for scoped stylesheet targeting and apply appearance stylesheet
        with silent_operation("settings_dialog_set_stylesheet"):
            self.setObjectName('settings_dialog')
            self.setStyleSheet("""
#settings_dialog QWidget#appearance_container { background-color: #4B4B4B; }
#settings_dialog QLabel, #settings_dialog QToolButton, #settings_dialog QCheckBox { color: #e0e0e0; }
#settings_dialog QLineEdit, #settings_dialog QComboBox, #settings_dialog QSpinBox, #settings_dialog QDoubleSpinBox {
    background-color: #3a3a3a;
    color: #f0f0f0;
    border: 1px solid #555555;
    padding: 4px 6px;
}
#settings_dialog QComboBox QAbstractItemView { background: #3a3a3a; color: #f0f0f0; selection-background-color: #4b4b4b; }
""")
        self.resize(640, 480)
        self._setup_ui()

    def _obfuscate_user_in_path(self, path: str) -> str:
        """Replace the user's home directory segment with '%USER%' for display in GUI fields.
        Only used for GUI display; never as an actual filesystem path.
        """
        try:
            if not path:
                return ''
            home = os.path.expanduser('~')
            # Normalize for comparison
            norm_path = os.path.normcase(os.path.normpath(path))
            norm_home = os.path.normcase(os.path.normpath(home))
            if norm_home and norm_path.startswith(norm_home):
                parent = os.path.dirname(home)
                # build obfuscated prefix (e.g., C:\Users\%USER%)
                obf_prefix = os.path.join(parent, '%USER%')
                rel = path[len(home):]
                # Ensure leading separator is preserved
                if rel.startswith(os.path.sep):
                    return obf_prefix + rel
                else:
                    return obf_prefix + os.path.sep + rel
            # fallback: try to hide a bare username segment in common Windows paths
            parts = path.split(os.path.sep)
            if len(parts) >= 3 and parts[1].lower() == 'users':
                parts[2] = '%USER%'
                return os.path.sep.join(parts)
            return path
        except Exception:
            return path

    def _shorten_and_obfuscate_path(self, path: str, max_len: int = 60) -> str:
        """Return an obfuscated and shortened version of `path` for display.

        - Replaces the user home segment with '%USER%' using _obfuscate_user_in_path.
        - If the result is longer than max_len, elide the middle keeping the start and end.
        This is only for GUI display; callers should keep the real full path in storage.
        """
        try:
            if not path:
                return ''
            ob = self._obfuscate_user_in_path(path)
            if len(ob) <= max_len:
                return ob
            # elide in the middle: keep head and tail
            keep = max(8, int(max_len * 0.35))
            head = ob[:keep]
            tail = ob[-(max_len - keep - 3):]
            return f"{head}...{tail}"
        except Exception:
            return self._obfuscate_user_in_path(path)

    def _setup_ui(self):
        v = QVBoxLayout()

        tabs = QTabWidget()

        # Snapshot canvas state to support cancellation reversion
        _snapshot = {
            'visualizer_lineage_mode': getattr(self.canvas, 'visualizer_lineage_mode', True),
            'color_lines_by_lineage': getattr(self.canvas, 'color_lines_by_lineage', True),
            'auto_layout_enabled': getattr(self.canvas, 'auto_layout_enabled', False),
            'connection_line_width': getattr(self.canvas, 'connection_line_width', 2),
            'undo_limit': getattr(self.canvas, 'undo_limit', 100),
            'notes_enabled': getattr(self.canvas, 'notes_enabled', False),
            'drag_to_link_mode': getattr(self.canvas, 'drag_to_link_mode', False),
            'default_focus_color': getattr(self.canvas, 'default_focus_color', None),
            'note_connection_curve_strength': getattr(self.canvas, 'note_connection_curve_strength', 1.0),
            'mutex_icon_display_scale': getattr(self.canvas, 'mutex_icon_display_scale', 1.0),
            'title_pill_mode': getattr(self.canvas, 'title_pill_mode', 'image'),
            'title_pill_image_path': getattr(self.canvas, 'title_pill_image_path', ''),
            'title_pill_padding': getattr(self.canvas, 'title_pill_padding', 8.0),
            'icon_view_mode': getattr(self.canvas, 'icon_view_mode', False),
            'icon_view_icon_max': getattr(self.canvas, 'icon_view_icon_max', 120),
            'icon_view_show_background': getattr(self.canvas, 'icon_view_show_background', True),
            'prefer_pillow_tga': getattr(self.canvas, 'prefer_pillow_tga', True),
            'icon_supersample_scale': getattr(self.canvas, 'icon_supersample_scale', 1.0),
            'mutex_icon_supersample_scale': getattr(self.canvas, 'mutex_icon_supersample_scale', 1.0),
            'focus_title_offset_x': getattr(self.canvas, 'focus_title_offset_x', 0),
            'focus_title_offset_y': getattr(self.canvas, 'focus_title_offset_y', 0),
            'focus_icon_offset_x': getattr(self.canvas, 'focus_icon_offset_x', 0),
            'focus_icon_offset_y': getattr(self.canvas, 'focus_icon_offset_y', 0),
            'focus_pill_offset_x': getattr(self.canvas, 'focus_pill_offset_x', 0),
            'focus_pill_offset_y': getattr(self.canvas, 'focus_pill_offset_y', 0),
            'event_title_offset_x': getattr(self.canvas, 'event_title_offset_x', 0),
            'event_title_offset_y': getattr(self.canvas, 'event_title_offset_y', 0),
            'event_desc_offset_x': getattr(self.canvas, 'event_desc_offset_x', 0),
            'event_desc_offset_y': getattr(self.canvas, 'event_desc_offset_y', 0),
            'event_options_offset_x': getattr(self.canvas, 'event_options_offset_x', 0),
            'event_options_offset_y': getattr(self.canvas, 'event_options_offset_y', 0),
        }

        def _restore_snapshot():
            try:
                for key, value in _snapshot.items():
                    try:
                        setattr(self.canvas, key, value)
                    except Exception:
                        pass
                _refresh_canvas_items()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        def _save_if_prefer_app():
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        def _refresh_canvas_items():
            try:
                for n in list(getattr(self.canvas, 'nodes', {}).values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for c in list(getattr(self.canvas, 'connections', [])):
                    try:
                        if hasattr(c, 'update_path'):
                            c.update_path()
                        else:
                            c.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.canvas.schedule_frame_update()
                except Exception:
                    try:
                        self.canvas.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # --- General tab ---
        gen_w = QWidget(); gen_l = QFormLayout()
        # Tree ID (app-level) — parent window stores this
        self.tree_id_edit = QLineEdit(getattr(self.canvas.parent, 'tree_id', ''))
        self.tree_id_edit.editingFinished.connect(self._on_tree_id)
        gen_l.addRow('Tree ID:', self.tree_id_edit)
        # Country tag
        self.country_edit = QLineEdit(getattr(self.canvas.parent, 'country_tag', 'TAG'))
        self.country_edit.editingFinished.connect(self._on_country_tag)
        gen_l.addRow('Country tag:', self.country_edit)
    # App base directory control (top)
        base_h = QHBoxLayout()
        real_base = getattr(self.canvas.parent, 'app_base_dir', '')
        self.app_base_edit = QLineEdit(self._obfuscate_user_in_path(real_base))
        self.app_base_edit.setReadOnly(True)
        base_btn = QPushButton('Change...')
        def _change_app_base():
            real_start = getattr(self.canvas.parent, 'app_base_dir', '') or os.getcwd()
            fn = QFileDialog.getExistingDirectory(self, 'Choose Application Base Folder', real_start)
            if fn:
                with silent_operation("settings_change_app_base"):
                    self.canvas.parent.app_base_dir = fn
                    ok = True
                    with silent_operation("settings_ensure_app_dirs"):
                        ok = self.canvas.parent.ensure_app_dirs()
                    if ok:
                        self.app_base_edit.setText(self._obfuscate_user_in_path(fn))
                    else:
                        QMessageBox.warning(self, 'Invalid Folder', 'Failed to prepare folders for the selected base directory.')
        base_btn.clicked.connect(_change_app_base)
        base_h.addWidget(self.app_base_edit); base_h.addWidget(base_btn)
        gen_l.addRow('Application Base folder:', base_h)

        # Settings file (after base)
        sp_h = QHBoxLayout()
        real_settings_path = getattr(self.canvas.parent, 'settings_path', '')
        self.settings_path_label = QLineEdit(self._obfuscate_user_in_path(real_settings_path))
        self.settings_path_label.setReadOnly(True)
        btn = QPushButton('Change...')
        btn.clicked.connect(self._change_settings_path)
        sp_h.addWidget(self.settings_path_label); sp_h.addWidget(btn)
        gen_l.addRow('Settings file:', sp_h)

        db_h = QHBoxLayout()
        real_db = getattr(self.canvas.parent, 'database_path', '')
        self.database_path_label = QLineEdit(self._obfuscate_user_in_path(real_db))
        self.database_path_label.setReadOnly(True)
        db_btn = QPushButton('Change...')
        db_btn.clicked.connect(self._change_database_path)
        db_h.addWidget(self.database_path_label); db_h.addWidget(db_btn)
        gen_l.addRow('Database file:', db_h)

        # Load / Save DB buttons
        db_ops = QHBoxLayout()
        load_db_btn = QPushButton('Load Database')
        save_db_btn = QPushButton('Save Database')
        load_db_btn.clicked.connect(self._do_load_database)
        save_db_btn.clicked.connect(self._do_save_database)
        db_ops.addWidget(load_db_btn); db_ops.addWidget(save_db_btn)
        gen_l.addRow(db_ops)

        # Icon library path and projects home path
        icon_path_h = QHBoxLayout()
        real_icon_path = getattr(self.canvas.parent, 'icon_library_path', '')
        self.icon_library_path_edit = QLineEdit(self._obfuscate_user_in_path(real_icon_path))
        self.icon_library_path_edit.setReadOnly(True)
        icon_path_btn = QPushButton('Change...')
        def _change_icon_library_path():
            real_start = getattr(self.canvas.parent, 'icon_library_path', '') or os.getcwd()
            fn = QFileDialog.getExistingDirectory(self, 'Choose Icon Library Folder', real_start)
            if fn:
                with silent_operation("settings_change_icon_library"):
                    self.canvas.parent.icon_library_path = fn
                    self.icon_library_path_edit.setText(self._obfuscate_user_in_path(fn))
                    # trigger a rescan of icon library on the main window
                    with silent_operation("settings_scan_icon_library"):
                        self.canvas.parent.scan_icon_library()
                    # Persist immediately if preferring app-wide settings
                    with silent_operation("settings_save_after_icon_library"):
                        if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                            self.canvas.parent.save_settings()
        icon_path_btn.clicked.connect(_change_icon_library_path)
        icon_path_h.addWidget(self.icon_library_path_edit); icon_path_h.addWidget(icon_path_btn)
        gen_l.addRow('Icon Library folder:', icon_path_h)

        proj_path_h = QHBoxLayout()
        real_projects = getattr(self.canvas.parent, 'projects_home_path', None)
        if not real_projects:
            abd = getattr(self.canvas.parent, 'app_base_dir', None)
            real_projects = os.path.join(abd, 'projects') if abd else os.getcwd()
        self.projects_home_path_edit = QLineEdit(self._obfuscate_user_in_path(real_projects))
        self.projects_home_path_edit.setReadOnly(True)
        proj_path_btn = QPushButton('Change...')
        def _change_projects_home_path():
            real_start = getattr(self.canvas.parent, 'projects_home_path', '')
            if not real_start:
                abd = getattr(self.canvas.parent, 'app_base_dir', None)
                real_start = os.path.join(abd, 'projects') if abd else os.getcwd()
            fn = QFileDialog.getExistingDirectory(self, 'Choose Projects Home Folder', real_start)
            if fn:
                with silent_operation("settings_change_projects_home"):
                    self.canvas.parent.projects_home_path = fn
                    self.projects_home_path_edit.setText(self._obfuscate_user_in_path(fn))
                    with silent_operation("settings_save_after_projects_home"):
                        if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                            self.canvas.parent.save_settings()
        proj_path_btn.clicked.connect(_change_projects_home_path)
        proj_path_h.addWidget(self.projects_home_path_edit); proj_path_h.addWidget(proj_path_btn)
        gen_l.addRow('Projects Home folder:', proj_path_h)

        # Logging controls
        log_h = QHBoxLayout()
        self.logging_enabled_chk = QCheckBox('Enable logging to console')
        self.logging_enabled_chk.setChecked(bool(getattr(self.canvas.parent, 'logging_enabled', False)))
        def _on_logging_toggled(chk):
            with silent_operation("settings_logging_toggled"):
                en = bool(chk)
                self.canvas.parent.logging_enabled = en
                lvl = getattr(self.canvas.parent, 'logging_level', 'INFO')
                level_val = getattr(logging, str(lvl).upper(), logging.INFO)
                if en:
                    logger.setLevel(level_val)
                    if not logger.handlers:
                        h = logging.StreamHandler()
                        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
                        logger.addHandler(h)
                else:
                    # remove handlers to silence logger (keep level)
                    for h in list(logger.handlers):
                        with silent_operation("settings_remove_logger_handler"):
                            logger.removeHandler(h)
                # Persist immediately if preferring app-wide settings
                with silent_operation("settings_save_after_logging_toggle"):
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
        self.logging_enabled_chk.toggled.connect(_on_logging_toggled)
        self.logging_level_combo = QComboBox()
        self.logging_level_combo.addItems(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
        with silent_operation("settings_init_logging_level_combo"):
            cur = getattr(self.canvas.parent, 'logging_level', 'INFO')
            idx = self.logging_level_combo.findText(str(cur).upper())
            if idx >= 0:
                self.logging_level_combo.setCurrentIndex(idx)
        def _on_log_level_changed(idx):
            with silent_operation("settings_log_level_changed"):
                lvl = self.logging_level_combo.currentText()
                self.canvas.parent.logging_level = lvl
                if getattr(self.canvas.parent, 'logging_enabled', False):
                    level_val = getattr(logging, lvl.upper(), logging.INFO)
                    logger.setLevel(level_val)
                with silent_operation("settings_save_after_log_level"):
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
        self.logging_level_combo.currentIndexChanged.connect(_on_log_level_changed)
        log_h.addWidget(self.logging_enabled_chk)
        log_h.addWidget(QLabel('Level:'))
        log_h.addWidget(self.logging_level_combo)

        # Autosave controls
        autosave_group = QGroupBox('Auto Save')
        autosave_layout = QFormLayout()
        autosave_group.setLayout(autosave_layout)

        self.autosave_chk = QCheckBox('Enable Auto Save')
        self.autosave_chk.setChecked(bool(getattr(self.canvas.parent, 'autosave_enabled', False)))
        autosave_interval_h = QHBoxLayout()
        self.autosave_interval_spin = QSpinBox(); self.autosave_interval_spin.setRange(1, 1440)
        self.autosave_interval_spin.setValue(int(getattr(self.canvas.parent, 'autosave_interval_min', 5)))
        autosave_interval_h.addWidget(self.autosave_interval_spin); autosave_interval_h.addWidget(QLabel('min'))
        self.autosave_overwrite_chk = QCheckBox('Overwrite single autosave file')
        self.autosave_overwrite_chk.setChecked(bool(getattr(self.canvas.parent, 'autosave_overwrite', True)))
        self.autosave_rotate_chk = QCheckBox('Rotate multiple autosave files')
        self.autosave_rotate_chk.setChecked(bool(getattr(self.canvas.parent, 'autosave_rotate', False)))
        self.autosave_rotate_spin = QSpinBox(); self.autosave_rotate_spin.setRange(2, 100)
        self.autosave_rotate_spin.setValue(int(getattr(self.canvas.parent, 'autosave_rotate_count', 6)))
        # Reflect mutual exclusivity initially
        with silent_operation("settings_autosave_mutual_exclusivity"):
            if bool(getattr(self.canvas.parent, 'autosave_overwrite', True)):
                self.autosave_rotate_chk.setChecked(False)
            if bool(getattr(self.canvas.parent, 'autosave_rotate', False)):
                self.autosave_overwrite_chk.setChecked(False)
            self.autosave_rotate_spin.setEnabled(bool(getattr(self.canvas.parent, 'autosave_rotate', False)))

        autosave_layout.addRow(self.autosave_chk)
        autosave_layout.addRow('Interval:', autosave_interval_h)
        autosave_layout.addRow(self.autosave_overwrite_chk)
        rotate_h = QHBoxLayout(); rotate_h.addWidget(self.autosave_rotate_chk); rotate_h.addWidget(QLabel('Keep:')); rotate_h.addWidget(self.autosave_rotate_spin)
        autosave_layout.addRow(rotate_h)

        def _on_autosave_toggled(chk):
            with silent_operation("settings_autosave_toggled"):
                self.canvas.parent.autosave_enabled = bool(chk)
                if self.canvas.parent.autosave_enabled:
                    self.canvas.parent._start_autosave_timer()
                else:
                    self.canvas.parent._stop_autosave_timer()
                # Persist autosave preference immediately
                with silent_operation("settings_save_after_autosave_toggle"):
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()

        def _on_autosave_interval(v):
            with silent_operation("settings_autosave_interval_changed"):
                self.canvas.parent.autosave_interval_min = int(v)
                if getattr(self.canvas.parent, 'autosave_enabled', False):
                    self.canvas.parent._start_autosave_timer()
                with silent_operation("settings_save_after_autosave_interval"):
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()

        def _on_autosave_overwrite(chk):
            with silent_operation("settings_autosave_overwrite"):
                self.canvas.parent.autosave_overwrite = bool(chk)
                # enforce exclusivity: turning on overwrite disables rotate
                if self.canvas.parent.autosave_overwrite:
                    self.canvas.parent.autosave_rotate = False
                    self.autosave_rotate_chk.blockSignals(True)
                    self.autosave_rotate_chk.setChecked(False)
                    with silent_operation("settings_autosave_unblock_rotate"):
                        self.autosave_rotate_chk.blockSignals(False)
                # update rotate count enablement
                with silent_operation("settings_autosave_update_rotate_spin"):
                    self.autosave_rotate_spin.setEnabled(bool(self.canvas.parent.autosave_rotate))
                with silent_operation("settings_save_after_autosave_overwrite"):
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()

        def _on_autosave_rotate(chk):
            with silent_operation("settings_autosave_rotate"):
                self.canvas.parent.autosave_rotate = bool(chk)
                # enforce exclusivity: turning on rotate disables overwrite
                if self.canvas.parent.autosave_rotate:
                    self.canvas.parent.autosave_overwrite = False
                    self.autosave_overwrite_chk.blockSignals(True)
                    self.autosave_overwrite_chk.setChecked(False)
                    with silent_operation("settings_autosave_unblock_overwrite"):
                        self.autosave_overwrite_chk.blockSignals(False)
                # update rotate count enablement
                with silent_operation("settings_autosave_update_rotate_spin2"):
                    self.autosave_rotate_spin.setEnabled(bool(self.canvas.parent.autosave_rotate))
                with silent_operation("settings_save_after_autosave_rotate"):
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()

        def _on_autosave_rotate_count(v):
            with silent_operation("settings_autosave_rotate_count"):
                self.canvas.parent.autosave_rotate_count = int(v)
                with silent_operation("settings_save_after_rotate_count"):
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()

        # connect signals
        with silent_operation("settings_connect_autosave_signals"):
            self.autosave_chk.toggled.connect(_on_autosave_toggled)
            self.autosave_interval_spin.valueChanged.connect(_on_autosave_interval)
            self.autosave_overwrite_chk.toggled.connect(_on_autosave_overwrite)
            self.autosave_rotate_chk.toggled.connect(_on_autosave_rotate)
            self.autosave_rotate_spin.valueChanged.connect(_on_autosave_rotate_count)

        gen_l.addRow(autosave_group)
        gen_w.setLayout(gen_l)

        # --- Performance tab (constructed now, added after Appearance/Advanced for expected order) ---
        perf_tab = QWidget()
        perf_form = QFormLayout(perf_tab)

        self.grid_visible_chk = QCheckBox('Show grid background')
        self.grid_visible_chk.setChecked(bool(getattr(self.canvas, '_grid_visible', True)))
        def _on_grid_visible(chk):
            with silent_operation("settings_grid_visible"):
                self.canvas.set_grid_visible(bool(chk))
                _save_if_prefer_app()
        self.grid_visible_chk.toggled.connect(_on_grid_visible)
        perf_form.addRow(self.grid_visible_chk)

        self.frames_enabled_chk = QCheckBox('Show frame outlines (debug)')
        self.frames_enabled_chk.setChecked(bool(getattr(self.canvas, 'frames_enabled', False)))
        def _on_frames_enabled(chk):
            with silent_operation("settings_frames_enabled"):
                self.canvas.frames_enabled = bool(chk)
                if not chk:
                    # clear frames when disabled
                    try:
                        self.canvas.clear_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # mark frames dirty so they update on next render
                try:
                    self.canvas._frames_dirty = True
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                _save_if_prefer_app()
                if chk:
                    # trigger immediate update when enabling
                    try:
                        self.canvas.update_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                _refresh_canvas_items()
        self.frames_enabled_chk.toggled.connect(_on_frames_enabled)
        perf_form.addRow(self.frames_enabled_chk)

        # Frame type toggles (sub-options for when frames are enabled)
        frame_types_group = QGroupBox('Frame Types (when frames enabled)')
        frame_types_layout = QVBoxLayout(frame_types_group)
        
        self.show_frame_labels_chk = QCheckBox('Show frame labels')
        self.show_frame_labels_chk.setChecked(bool(getattr(self.canvas, 'show_frame_labels', True)))
        def _on_frame_labels(chk):
            with silent_operation("settings_frame_labels"):
                self.canvas.show_frame_labels = bool(chk)
                self.canvas._frames_dirty = True
                _save_if_prefer_app()
                if self.canvas.frames_enabled:
                    try:
                        self.canvas.update_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.show_frame_labels_chk.toggled.connect(_on_frame_labels)
        frame_types_layout.addWidget(self.show_frame_labels_chk)
        
        self.show_network_frames_chk = QCheckBox('Show network frames')
        self.show_network_frames_chk.setChecked(bool(getattr(self.canvas, 'show_network_frames', True)))
        def _on_network_frames(chk):
            with silent_operation("settings_network_frames"):
                self.canvas.show_network_frames = bool(chk)
                self.canvas._frames_dirty = True
                _save_if_prefer_app()
                if self.canvas.frames_enabled:
                    try:
                        self.canvas.update_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.show_network_frames_chk.toggled.connect(_on_network_frames)
        frame_types_layout.addWidget(self.show_network_frames_chk)
        
        self.show_layer_frames_chk = QCheckBox('Show layer frames')
        self.show_layer_frames_chk.setChecked(bool(getattr(self.canvas, 'show_layer_frames', True)))
        def _on_layer_frames(chk):
            with silent_operation("settings_layer_frames"):
                self.canvas.show_layer_frames = bool(chk)
                self.canvas._frames_dirty = True
                _save_if_prefer_app()
                if self.canvas.frames_enabled:
                    try:
                        self.canvas.update_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.show_layer_frames_chk.toggled.connect(_on_layer_frames)
        frame_types_layout.addWidget(self.show_layer_frames_chk)
        
        self.show_subtree_frames_chk = QCheckBox('Show subtree frames')
        self.show_subtree_frames_chk.setChecked(bool(getattr(self.canvas, 'show_subtree_frames', True)))
        def _on_subtree_frames(chk):
            with silent_operation("settings_subtree_frames"):
                self.canvas.show_subtree_frames = bool(chk)
                self.canvas._frames_dirty = True
                _save_if_prefer_app()
                if self.canvas.frames_enabled:
                    try:
                        self.canvas.update_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.show_subtree_frames_chk.toggled.connect(_on_subtree_frames)
        frame_types_layout.addWidget(self.show_subtree_frames_chk)
        
        self.show_lineage_frames_chk = QCheckBox('Show lineage frames')
        self.show_lineage_frames_chk.setChecked(bool(getattr(self.canvas, 'show_lineage_frames', True)))
        def _on_lineage_frames(chk):
            with silent_operation("settings_lineage_frames"):
                self.canvas.show_lineage_frames = bool(chk)
                self.canvas._frames_dirty = True
                _save_if_prefer_app()
                if self.canvas.frames_enabled:
                    try:
                        self.canvas.update_frames()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.show_lineage_frames_chk.toggled.connect(_on_lineage_frames)
        frame_types_layout.addWidget(self.show_lineage_frames_chk)
        
        perf_form.addRow(frame_types_group)

        # Rendering detail controls
        detail_group = QGroupBox('Detail Levels')
        detail_form = QFormLayout(detail_group)

        self.dynamic_scaling_chk = QCheckBox('Enable dynamic title/icon scaling')
        self.dynamic_scaling_chk.setChecked(bool(getattr(self.canvas, 'enable_dynamic_title_icon_scaling', True)))
        def _on_dynamic_scaling(chk):
            with silent_operation("settings_dynamic_scaling"):
                self.canvas.enable_dynamic_title_icon_scaling = bool(chk)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.dynamic_scaling_chk.toggled.connect(_on_dynamic_scaling)
        detail_form.addRow(self.dynamic_scaling_chk)

        self.dynamic_zoom_spin = QDoubleSpinBox(); self.dynamic_zoom_spin.setRange(0.0, 2.0); self.dynamic_zoom_spin.setSingleStep(0.05)
        self.dynamic_zoom_spin.setValue(float(getattr(self.canvas, 'title_icon_scale_zoom_threshold', 0.3)))
        def _on_dynamic_zoom(v):
            with silent_operation("settings_dynamic_zoom_threshold"):
                self.canvas.title_icon_scale_zoom_threshold = float(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.dynamic_zoom_spin.valueChanged.connect(_on_dynamic_zoom)
        detail_form.addRow('Scaling zoom threshold:', self.dynamic_zoom_spin)

        self.dynamic_max_spin = QDoubleSpinBox(); self.dynamic_max_spin.setRange(1.0, 5.0); self.dynamic_max_spin.setSingleStep(0.1)
        self.dynamic_max_spin.setValue(float(getattr(self.canvas, 'title_icon_scale_max_multiplier', 2.5)))
        def _on_dynamic_max(v):
            with silent_operation("settings_dynamic_max_multiplier"):
                self.canvas.title_icon_scale_max_multiplier = float(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.dynamic_max_spin.valueChanged.connect(_on_dynamic_max)
        detail_form.addRow('Max scale multiplier:', self.dynamic_max_spin)

        self.simple_render_spin = QDoubleSpinBox(); self.simple_render_spin.setRange(0.0, 2.0); self.simple_render_spin.setSingleStep(0.05)
        self.simple_render_spin.setValue(float(getattr(self.canvas, 'simple_render_zoom_threshold', 0.0)))
        def _on_simple_render(v):
            with silent_operation("settings_simple_render_threshold"):
                self.canvas.simple_render_zoom_threshold = float(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.simple_render_spin.valueChanged.connect(_on_simple_render)
        detail_form.addRow('Simple render zoom threshold:', self.simple_render_spin)

        self.connection_lod_spin = QDoubleSpinBox(); self.connection_lod_spin.setRange(0.0, 1.0); self.connection_lod_spin.setSingleStep(0.05)
        self.connection_lod_spin.setValue(float(getattr(self.canvas, 'connection_lod_threshold', 0.0)))
        def _on_connection_lod(v):
            with silent_operation("settings_connection_lod"):
                self.canvas.connection_lod_threshold = float(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.connection_lod_spin.valueChanged.connect(_on_connection_lod)
        detail_form.addRow('Connection LOD threshold:', self.connection_lod_spin)

        perf_form.addRow(detail_group)

        # Culling controls
        cull_group = QGroupBox('Culling')
        cull_form = QFormLayout(cull_group)

        self.culling_enabled_chk = QCheckBox('Enable viewport culling')
        self.culling_enabled_chk.setChecked(bool(getattr(self.canvas, 'culling_enabled', True)))
        def _on_culling_enabled(chk):
            with silent_operation("settings_culling_enabled"):
                self.canvas.culling_enabled = bool(chk)
                _save_if_prefer_app()
                try:
                    self.canvas._perform_cull()
                except Exception:
                    _refresh_canvas_items()
        self.culling_enabled_chk.toggled.connect(_on_culling_enabled)
        cull_form.addRow(self.culling_enabled_chk)

        self.culling_min_nodes_spin = QSpinBox(); self.culling_min_nodes_spin.setRange(0, 20000)
        self.culling_min_nodes_spin.setValue(int(getattr(self.canvas, 'culling_min_nodes', 150)))
        def _on_cull_min_nodes(v):
            with silent_operation("settings_cull_min_nodes"):
                self.canvas.culling_min_nodes = int(v)
                _save_if_prefer_app()
        self.culling_min_nodes_spin.valueChanged.connect(_on_cull_min_nodes)
        cull_form.addRow('Minimum nodes to cull:', self.culling_min_nodes_spin)

        self.culling_min_conns_spin = QSpinBox(); self.culling_min_conns_spin.setRange(0, 20000)
        self.culling_min_conns_spin.setValue(int(getattr(self.canvas, 'culling_min_connections', 250)))
        def _on_cull_min_conns(v):
            with silent_operation("settings_cull_min_connections"):
                self.canvas.culling_min_connections = int(v)
                _save_if_prefer_app()
        self.culling_min_conns_spin.valueChanged.connect(_on_cull_min_conns)
        cull_form.addRow('Minimum connections to cull:', self.culling_min_conns_spin)

        self.cull_margin_spin = QSpinBox(); self.cull_margin_spin.setRange(50, 5000)
        self.cull_margin_spin.setValue(int(getattr(self.canvas, '_cull_margin', 300)))
        def _on_cull_margin(v):
            with silent_operation("settings_cull_margin"):
                self.canvas._cull_margin = int(v)
                _save_if_prefer_app()
                try:
                    self.canvas._perform_cull()
                except Exception:
                    _refresh_canvas_items()
        self.cull_margin_spin.valueChanged.connect(_on_cull_margin)
        cull_form.addRow('Viewport cull margin (px):', self.cull_margin_spin)

        self.zoom_culling_chk = QCheckBox('Enable zoom-based culling')
        self.zoom_culling_chk.setChecked(bool(getattr(self.canvas, 'zoom_culling_enabled', True)))
        def _on_zoom_culling(chk):
            with silent_operation("settings_zoom_culling"):
                self.canvas.zoom_culling_enabled = bool(chk)
                _save_if_prefer_app()
                try:
                    self.canvas._perform_cull()
                except Exception:
                    _refresh_canvas_items()
        self.zoom_culling_chk.toggled.connect(_on_zoom_culling)
        cull_form.addRow(self.zoom_culling_chk)

        self.zoom_cull_threshold_spin = QDoubleSpinBox(); self.zoom_cull_threshold_spin.setRange(0.0, 2.0); self.zoom_cull_threshold_spin.setSingleStep(0.05)
        self.zoom_cull_threshold_spin.setValue(float(getattr(self.canvas, 'zoom_cull_threshold', 0.3)))
        def _on_zoom_threshold(v):
            with silent_operation("settings_zoom_cull_threshold"):
                self.canvas.zoom_cull_threshold = float(v)
                _save_if_prefer_app()
                try:
                    self.canvas._perform_cull()
                except Exception:
                    _refresh_canvas_items()
        self.zoom_cull_threshold_spin.valueChanged.connect(_on_zoom_threshold)
        cull_form.addRow('Zoom cull threshold:', self.zoom_cull_threshold_spin)

        self.zoom_cull_margin_factor_spin = QDoubleSpinBox(); self.zoom_cull_margin_factor_spin.setRange(0.0, 1.0); self.zoom_cull_margin_factor_spin.setSingleStep(0.05)
        self.zoom_cull_margin_factor_spin.setValue(float(getattr(self.canvas, 'zoom_cull_margin_factor', 0.4)))
        def _on_zoom_margin_factor(v):
            with silent_operation("settings_zoom_cull_margin_factor"):
                self.canvas.zoom_cull_margin_factor = float(v)
                _save_if_prefer_app()
                try:
                    self.canvas._perform_cull()
                except Exception:
                    _refresh_canvas_items()
        self.zoom_cull_margin_factor_spin.valueChanged.connect(_on_zoom_margin_factor)
        cull_form.addRow('Zoom cull margin factor:', self.zoom_cull_margin_factor_spin)

        self.zoom_cull_min_margin_spin = QSpinBox(); self.zoom_cull_min_margin_spin.setRange(0, 2000)
        self.zoom_cull_min_margin_spin.setValue(int(getattr(self.canvas, 'zoom_cull_min_margin', 100)))
        def _on_zoom_min_margin(v):
            with silent_operation("settings_zoom_cull_min_margin"):
                self.canvas.zoom_cull_min_margin = int(v)
                _save_if_prefer_app()
                try:
                    self.canvas._perform_cull()
                except Exception:
                    _refresh_canvas_items()
        self.zoom_cull_min_margin_spin.valueChanged.connect(_on_zoom_min_margin)
        cull_form.addRow('Zoom cull min margin (px):', self.zoom_cull_min_margin_spin)

        perf_form.addRow(cull_group)

        # --- Appearance tab (combined visuals & icons) ---
        appearance_page = QWidget()
        appearance_layout = QVBoxLayout(appearance_page)
        appearance_layout.setContentsMargins(0, 0, 0, 0)
        appearance_layout.setSpacing(0)

        appearance_scroll = QScrollArea()
        appearance_scroll.setWidgetResizable(True)
        appearance_layout.addWidget(appearance_scroll)

        appearance_container = QWidget()
        appearance_scroll.setWidget(appearance_container)
        # Scope this container so the dialog stylesheet can target it
        appearance_container.setObjectName('appearance_container')
        sections_layout = QVBoxLayout(appearance_container)
        sections_layout.setContentsMargins(12, 12, 12, 12)
        sections_layout.setSpacing(12)

        # Organize appearance controls into logical, titled groups for clarity
        try:
            # Title & Pill appearance
            title_group = QGroupBox('Title and Pill')
        except Exception as e:
            logger.error("Focus Title Pill failed - See %s", e)
            title_group = None  # ensure variable exists even if creation failed

        if title_group is not None:
            title_group_layout = QFormLayout()
            title_group.setLayout(title_group_layout)

        self.title_size_spin = QSpinBox(); self.title_size_spin.setRange(8, 48)
        self.title_size_spin.setValue(int(getattr(self.canvas, 'focus_title_font_size', 14)))
        self.title_size_spin.valueChanged.connect(self._on_title_size)
        title_group_layout.addRow('Focus title size (pt):', self.title_size_spin)

        self.title_outline_chk = QCheckBox('Outline title text')
        self.title_outline_chk.setChecked(bool(getattr(self.canvas, 'title_outline_enabled', True)))
        self.title_outline_chk.toggled.connect(self._on_title_outline)
        title_group_layout.addRow(self.title_outline_chk)

        self.title_outline_th = QSpinBox(); self.title_outline_th.setRange(1, 4)
        self.title_outline_th.setValue(int(getattr(self.canvas, 'title_outline_thickness', 1)))
        self.title_outline_th.valueChanged.connect(self._on_title_outline_th)
        title_group_layout.addRow('Outline thickness:', self.title_outline_th)

        # Show/hide node IDs (not the title)
        self.render_node_ids_chk = QCheckBox('Show node IDs')
        self.render_node_ids_chk.setChecked(bool(getattr(self.canvas, 'render_node_ids', True)))
        def _on_render_node_ids(chk):
            with silent_operation("settings_render_node_ids"):
                self.canvas.render_node_ids = bool(chk)
                with silent_operation("settings_save_after_render_node_ids"):
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                with silent_operation("settings_refresh_after_render_node_ids"):
                    self.canvas.schedule_frame_update()
        self.render_node_ids_chk.toggled.connect(_on_render_node_ids)
        title_group_layout.addRow(self.render_node_ids_chk)

        # --- Country Event appearance group ---
        country_group = QGroupBox('Country Event Rendering')
        country_group_layout = QFormLayout()
        country_group.setLayout(country_group_layout)

        # Enable country event mode
        self.country_event_mode_chk = QCheckBox('Enable country event rendering mode')
        self.country_event_mode_chk.setChecked(bool(getattr(self.canvas, 'country_event_mode', False)))
        def _on_country_event_mode(chk):
            with silent_operation("settings_country_event_mode"):
                self.canvas.country_event_mode = bool(chk)
                # Persist if app-wide preference is enabled
                with silent_operation("settings_save_after_country_event_mode"):
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                # Refresh canvas
                with silent_operation("settings_refresh_after_country_event_mode"):
                    self.canvas.schedule_frame_update()
        self.country_event_mode_chk.toggled.connect(_on_country_event_mode)
        country_group_layout.addRow(self.country_event_mode_chk)

        # Background path control
        bg_h = QHBoxLayout()
        real_bg = getattr(self.canvas.parent, 'country_event_bg_path', '') or getattr(self.canvas.parent, 'country_event_bg', '')
        # If no explicit user path is set and we have a packaged asset, show a friendly 'Embedded' label
        try:
            if not real_bg:
                packaged = os.path.join(os.path.dirname(__file__), '_assets', 'country_event_bg.png')
                if os.path.exists(packaged):
                    display_text = 'Embedded (packaged asset)'
                    tooltip_text = 'Using embedded packaged asset'
                else:
                    display_text = self._shorten_and_obfuscate_path(real_bg)
                    tooltip_text = self._obfuscate_user_in_path(real_bg)
            else:
                display_text = self._shorten_and_obfuscate_path(real_bg)
                tooltip_text = self._obfuscate_user_in_path(real_bg)
        except Exception:
            display_text = self._shorten_and_obfuscate_path(real_bg)
            tooltip_text = self._obfuscate_user_in_path(real_bg)

        self.country_event_bg_edit = QLineEdit(display_text)
        # show full obfuscated path in tooltip for clarity (or embedded text)
        self.country_event_bg_edit.setToolTip(tooltip_text)
        self.country_event_bg_edit.setReadOnly(True)
        bg_btn = QPushButton('Change...')
        def _change_country_bg():
            real_start = getattr(self.canvas.parent, 'country_event_bg_path', '') or os.getcwd()
            fn, _ = QFileDialog.getOpenFileName(self, 'Choose Country Event Background', real_start, 'Images (*.png *.jpg *.bmp *.tga *.dds)')
            if fn:
                try:
                    # store the real absolute path on both the parent and the canvas so runtime uses it
                    fn_abs = os.path.abspath(fn)
                    try:
                        self.canvas.parent.country_event_bg_path = fn_abs
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        # canvas may prefer storing the path directly as well
                        setattr(self.canvas, 'country_event_bg_path', fn_abs)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    # display a shortened, obfuscated version in the field
                    self.country_event_bg_edit.setText(self._shorten_and_obfuscate_path(fn_abs))
                    self.country_event_bg_edit.setToolTip(self._obfuscate_user_in_path(fn_abs))

                    # Persist preference if requested
                    try:
                        if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                            self.canvas.parent.save_settings()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    # Clear per-EventNode cached masks so new images are used on next paint
                    try:
                        for en in list(getattr(self.canvas, 'event_nodes', {}).values()):
                            try:
                                if hasattr(en, '_country_art_mask_for'):
                                    en._country_art_mask_for = None
                                if hasattr(en, '_country_art_mask_path'):
                                    en._country_art_mask_path = None
                                try:
                                    en.update()
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    # Request canvas refresh
                    try:
                        self.canvas.schedule_frame_update()
                    except Exception:
                        try:
                            self.canvas.update()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception:
                    QMessageBox.warning(self, 'Error', 'Failed to set background image.')
        bg_btn.clicked.connect(_change_country_bg)
        bg_h.addWidget(self.country_event_bg_edit); bg_h.addWidget(bg_btn)
        country_group_layout.addRow('Background image:', bg_h)

        # Overlay path control
        ov_h = QHBoxLayout()
        real_ov = getattr(self.canvas.parent, 'country_event_overlay_path', '') or getattr(self.canvas.parent, 'country_event_overlay', '')
        try:
            if not real_ov:
                packaged_ov = os.path.join(os.path.dirname(__file__), '_assets', 'country_event_overlay.png')
                if os.path.exists(packaged_ov):
                    display_ov = 'Embedded (packaged asset)'
                    tooltip_ov = 'Using embedded packaged overlay asset'
                else:
                    display_ov = self._shorten_and_obfuscate_path(real_ov)
                    tooltip_ov = self._obfuscate_user_in_path(real_ov)
            else:
                display_ov = self._shorten_and_obfuscate_path(real_ov)
                tooltip_ov = self._obfuscate_user_in_path(real_ov)
        except Exception:
            display_ov = self._shorten_and_obfuscate_path(real_ov)
            tooltip_ov = self._obfuscate_user_in_path(real_ov)
        self.country_event_ov_edit = QLineEdit(display_ov)
        self.country_event_ov_edit.setToolTip(tooltip_ov)
        self.country_event_ov_edit.setReadOnly(True)
        ov_btn = QPushButton('Change...')
        def _change_country_ov():
            real_start = getattr(self.canvas.parent, 'country_event_overlay_path', '') or os.getcwd()
            fn, _ = QFileDialog.getOpenFileName(self, 'Choose Country Event Overlay', real_start, 'Images (*.png *.jpg *.bmp *.tga *.dds)')
            if fn:
                try:
                    # persist absolute path to both parent and canvas attributes
                    fn_abs = os.path.abspath(fn)
                    try:
                        self.canvas.parent.country_event_overlay_path = fn_abs
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        setattr(self.canvas, 'country_event_overlay_path', fn_abs)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    self.country_event_ov_edit.setText(self._shorten_and_obfuscate_path(fn_abs))
                    self.country_event_ov_edit.setToolTip(self._obfuscate_user_in_path(fn_abs))

                    try:
                        if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                            self.canvas.parent.save_settings()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    # Clear per-EventNode cached masks so new images are used on next paint
                    try:
                        for en in list(getattr(self.canvas, 'event_nodes', {}).values()):
                            try:
                                if hasattr(en, '_country_art_mask_for'):
                                    en._country_art_mask_for = None
                                if hasattr(en, '_country_art_mask_path'):
                                    en._country_art_mask_path = None
                                try:
                                    en.update()
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                    try:
                        self.canvas.schedule_frame_update()
                    except Exception:
                        try:
                            self.canvas.update()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception:
                    QMessageBox.warning(self, 'Error', 'Failed to set overlay image.')
        ov_btn.clicked.connect(_change_country_ov)
        ov_h.addWidget(self.country_event_ov_edit); ov_h.addWidget(ov_btn)
        country_group_layout.addRow('Overlay image:', ov_h)

        # SSAA control (double spin)
        self.country_event_ssaa_spin = QDoubleSpinBox(); self.country_event_ssaa_spin.setRange(1.0, 4.0); self.country_event_ssaa_spin.setSingleStep(0.25)
        try:
            cur_ssaa = float(getattr(self.canvas, 'country_event_ssaa', getattr(self.canvas, 'event_supersample_scale', 1.0)) or 1.0)
        except Exception:
            cur_ssaa = 1.0
        self.country_event_ssaa_spin.setValue(cur_ssaa)
        def _on_ssaa_changed(val):
            try:
                self.canvas.country_event_ssaa = float(val)
                try:
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.canvas.schedule_frame_update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.country_event_ssaa_spin.valueChanged.connect(_on_ssaa_changed)
        country_group_layout.addRow('Image SSAA scale:', self.country_event_ssaa_spin)

        # Title offset (vertical nudge) for country event title
        try:
            cur_off = float(getattr(self.canvas, 'country_event_title_offset', 10.0))
        except Exception:
            cur_off = 10.0
        self.country_event_title_offset_spin = QDoubleSpinBox(); self.country_event_title_offset_spin.setRange(-40.0, 40.0); self.country_event_title_offset_spin.setSingleStep(1.0)
        self.country_event_title_offset_spin.setValue(cur_off)
        def _on_title_offset(v):
            try:
                self.canvas.country_event_title_offset = float(v)
                try:
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.canvas.schedule_frame_update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            # Update any live EventNode items so the one being rendered refreshes immediately
            try:
                for en in list(getattr(self.canvas, 'event_nodes', {}).values()):
                    try:
                        en.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.canvas.schedule_frame_update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            cur_title_fs = int(getattr(self.canvas, 'event_title_font_size', getattr(self.canvas, 'country_event_title_font_size', 14)))
        except Exception:
            cur_title_fs = 14
        self.event_title_font_spin = QSpinBox(); self.event_title_font_spin.setRange(6, 48); self.event_title_font_spin.setValue(cur_title_fs)
        def _on_event_title_font(v):
            try:
                self.canvas.event_title_font_size = int(v)
                try:
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    # Update live EventNode(s) so the edited/preview node refreshes immediately
                    try:
                        for en in list(getattr(self.canvas, 'event_nodes', {}).values()):
                            try:
                                en.update()
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.canvas.schedule_frame_update()
                except Exception:
                    try:
                        self.canvas.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.event_title_font_spin.valueChanged.connect(_on_event_title_font)
        country_group_layout.addRow('Event title font size (pt):', self.event_title_font_spin)

        try:
            cur_desc_fs = int(getattr(self.canvas, 'event_desc_font_size', 10))
        except Exception:
            cur_desc_fs = 10
        self.event_desc_font_spin = QSpinBox(); self.event_desc_font_spin.setRange(6, 48); self.event_desc_font_spin.setValue(cur_desc_fs)
        def _on_event_desc_font(v):
            try:
                self.canvas.event_desc_font_size = int(v)
                try:
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    try:
                        for en in list(getattr(self.canvas, 'event_nodes', {}).values()):
                            try:
                                en.update()
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.canvas.schedule_frame_update()
                except Exception:
                    try:
                        self.canvas.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.event_desc_font_spin.valueChanged.connect(_on_event_desc_font)
        country_group_layout.addRow('Event description font size (pt):', self.event_desc_font_spin)

        try:
            cur_opt_fs = int(getattr(self.canvas, 'event_options_font_size', 10))
        except Exception:
            cur_opt_fs = 10
        self.event_opt_font_spin = QSpinBox(); self.event_opt_font_spin.setRange(6, 48); self.event_opt_font_spin.setValue(cur_opt_fs)
        def _on_event_opt_font(v):
            try:
                self.canvas.event_options_font_size = int(v)
                try:
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    try:
                        for en in list(getattr(self.canvas, 'event_nodes', {}).values()):
                            try:
                                en.update()
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.canvas.schedule_frame_update()
                except Exception:
                    try:
                        self.canvas.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.event_opt_font_spin.valueChanged.connect(_on_event_opt_font)
        country_group_layout.addRow('Event options font size (pt):', self.event_opt_font_spin)

        # Add the group to appearance sections
        sections_layout.addWidget(title_group)
        sections_layout.addWidget(country_group)
        # --- Render Stack Positioning (per-object X/Y offsets) ---
        rs_group = QGroupBox('Render Stack Positioning')
        rs_layout = QFormLayout()
        rs_group.setLayout(rs_layout)

        # small helper to create X/Y controls (range -200..200)
        def _make_xy_pair(x_val, y_val):
            h = QHBoxLayout()
            sx = QSpinBox(); sx.setRange(-200, 200); sx.setValue(int(x_val))
            sy = QSpinBox(); sy.setRange(-200, 200); sy.setValue(int(y_val))
            h.addWidget(QLabel('X:')); h.addWidget(sx); h.addSpacing(8); h.addWidget(QLabel('Y:')); h.addWidget(sy)
            return h, sx, sy

        def _save_and_refresh_canvas():
            try:
                # Persist canvas appearance changes immediately (per-project or app-wide)
                try:
                    if hasattr(self.canvas.parent, 'save_settings'):
                        self.canvas.parent.save_settings()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                # Force a visual refresh: update nodes and connections immediately
                for n in list(getattr(self.canvas, 'nodes', {}).values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # Also update EventNode instances which are stored separately
                for n in list(getattr(self.canvas, 'event_nodes', {}).values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                for c in list(getattr(self.canvas, 'connections', [])):
                    try:
                        if hasattr(c, 'update_path'):
                            c.update_path()
                        else:
                            c.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # schedule a frame update as well
                try:
                    self.canvas.schedule_frame_update()
                except Exception:
                    try:
                        self.canvas.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Items to expose: focus, focus title, focus icon, focus pill, event title, event description, event options
        mappings = [
            ('Focus title', 'focus_title_offset_x', 'focus_title_offset_y'),
            ('Focus icon', 'focus_icon_offset_x', 'focus_icon_offset_y'),
            ('Focus pill', 'focus_pill_offset_x', 'focus_pill_offset_y'),
            ('Event title', 'event_title_offset_x', 'event_title_offset_y'),
            ('Event description', 'event_desc_offset_x', 'event_desc_offset_y'),
            ('Event options', 'event_options_offset_x', 'event_options_offset_y'),
        ]

        # store references to the per-item X/Y spinboxes so we can re-sync UI later
        try:
            self._render_xy_controls = {}
        except Exception:
            self._render_xy_controls = {}

        for label, kx, ky in mappings:
            cur_x = int(getattr(self.canvas, kx, 0))
            cur_y = int(getattr(self.canvas, ky, 0))
            pair_h, sx, sy = _make_xy_pair(cur_x, cur_y)
            # For Event title we only expose the Y control because the X position is auto-centered
            try:
                if kx == 'event_title_offset_x':
                    try:
                        sx.setVisible(False)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    try:
                        # hide the leading 'X:' label which is the first widget in the pair layout
                        lbl = pair_h.itemAt(0).widget()
                        if lbl is not None:
                            lbl.setVisible(False)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            def _make_handler(kx_local, ky_local, sx_local, sy_local):
                def _handler(_=None):
                    try:
                        setattr(self.canvas, kx_local, int(sx_local.value()))
                        setattr(self.canvas, ky_local, int(sy_local.value()))
                        _save_and_refresh_canvas()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return _handler
            h = _make_handler(kx, ky, sx, sy)
            sx.valueChanged.connect(h); sy.valueChanged.connect(h)
            try:
                # store control refs keyed by the canvas property names
                self._render_xy_controls[(kx, ky)] = (sx, sy)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            rs_layout.addRow(label + ':', pair_h)

        sections_layout.addWidget(rs_group)
        # continue adding other groups below (existing code expects title_group to be added earlier)

        # Title pill style (default/image/none)
        self.title_pill_mode_combo = QComboBox(); self.title_pill_mode_combo.addItems(['Default (rounded)', 'Image', 'None'])
        cur_mode = str(getattr(self.canvas, 'title_pill_mode', 'default')).lower()
        self.title_pill_mode_combo.setCurrentIndex({'default': 0, 'image': 1, 'none': 2}.get(cur_mode, 0))
        self.title_pill_mode_combo.currentIndexChanged.connect(self._on_title_pill_mode)
        title_group_layout.addRow('Title pill style:', self.title_pill_mode_combo)

        pill_h = QHBoxLayout()
        self.title_pill_path_edit = QLineEdit(str(getattr(self.canvas, 'title_pill_image_path', '') or ''))
        pill_btn = QPushButton('Browse...')
        def _browse_title_pill():
            fn, _ = QFileDialog.getOpenFileName(self, 'Choose pill image', os.getcwd(), 'Images (*.png *.jpg *.jpeg *.bmp *.tga *.dds)')
            if fn:
                self.title_pill_path_edit.setText(fn)
                self._on_title_pill_image()
        pill_btn.clicked.connect(_browse_title_pill)
        pill_h.addWidget(self.title_pill_path_edit); pill_h.addWidget(pill_btn)
        title_group_layout.addRow('Title pill image:', pill_h)

        # Title pill padding
        self.title_pill_padding_spin = QDoubleSpinBox(); self.title_pill_padding_spin.setRange(0.0, 64.0); self.title_pill_padding_spin.setSingleStep(0.5)
        try:
            self.title_pill_padding_spin.setValue(float(getattr(self.canvas, 'title_pill_padding', 8.0)))
        except Exception:
            self.title_pill_padding_spin.setValue(8.0)
        def _on_pill_padding(v):
            try:
                self.canvas.title_pill_padding = float(v)
                for n in list(self.canvas.nodes.values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # refresh EventNode instances as well
                for n in list(getattr(self.canvas, 'event_nodes', {}).values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.title_pill_padding_spin.valueChanged.connect(_on_pill_padding)
        title_group_layout.addRow('Title pill padding (px):', self.title_pill_padding_spin)

        # Icons group
        icons_group = QGroupBox('Icons')
        icons_layout = QFormLayout()
        icons_group.setLayout(icons_layout)

        self.icon_view_chk = QCheckBox('Enable Icon View Mode')
        self.icon_view_chk.setChecked(bool(getattr(self.canvas, 'icon_view_mode', False)))
        self.icon_view_chk.toggled.connect(self._on_icon_view_toggle)
        icons_layout.addRow(self.icon_view_chk)

        self.icon_max_spin = QSpinBox(); self.icon_max_spin.setRange(16, 512)
        self.icon_max_spin.setValue(int(getattr(self.canvas, 'icon_view_icon_max', 120)))
        self.icon_max_spin.valueChanged.connect(self._on_icon_max)
        icons_layout.addRow('Max icon size (px):', self.icon_max_spin)

        self.icon_bg_chk = QCheckBox('Show icon background')
        self.icon_bg_chk.setChecked(bool(getattr(self.canvas, 'icon_view_show_background', False)))
        self.icon_bg_chk.toggled.connect(self._on_icon_bg)
        icons_layout.addRow(self.icon_bg_chk)

        self.prefer_pillow_chk = QCheckBox('Prefer Pillow for TGA/DDS')
        self.prefer_pillow_chk.setChecked(bool(getattr(self.canvas, 'prefer_pillow_tga', True)))
        self.prefer_pillow_chk.toggled.connect(self._on_prefer_pillow)
        icons_layout.addRow(self.prefer_pillow_chk)

        self.icon_ssaa_spin = QDoubleSpinBox(); self.icon_ssaa_spin.setRange(1.0, 4.0); self.icon_ssaa_spin.setSingleStep(0.5)
        try:
            self.icon_ssaa_spin.setValue(float(getattr(self.canvas, 'icon_supersample_scale', 1.0)))
        except Exception:
            self.icon_ssaa_spin.setValue(1.0)
        self.icon_ssaa_spin.valueChanged.connect(self._on_icon_ssaa)
        icons_layout.addRow('Icon supersample (SSAA):', self.icon_ssaa_spin)

        # Mutex icon rendering controls
        self.mutex_icon_ssaa_spin = QDoubleSpinBox(); self.mutex_icon_ssaa_spin.setRange(1.0, 4.0); self.mutex_icon_ssaa_spin.setSingleStep(0.25)
        try:
            self.mutex_icon_ssaa_spin.setValue(float(getattr(self.canvas, 'mutex_icon_supersample_scale', 1.0)))
        except Exception:
            self.mutex_icon_ssaa_spin.setValue(1.0)
        def _on_mutex_ssaa(v):
            try:
                self.canvas.mutex_icon_supersample_scale = float(v)
                # refresh any connections so icons are repainted
                for c in list(getattr(self.canvas, 'connections', [])):
                    try:
                        if hasattr(c, 'update_path'):
                            c.update_path()
                        else:
                            c.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.canvas.schedule_frame_update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.mutex_icon_ssaa_spin.valueChanged.connect(_on_mutex_ssaa)
        icons_layout.addRow('Mutex icon SSAA:', self.mutex_icon_ssaa_spin)

        sections_layout.addWidget(icons_group)

        # Add sections container to scroll area
        sections_layout.addStretch(1)

        appearance_page.setLayout(appearance_layout)

        # --- Advanced tab ---
        adv_tab = QWidget()
        adv_form = QFormLayout(adv_tab)

        self.lineage_mode_chk = QCheckBox('Visualizer lineage mode')
        self.lineage_mode_chk.setChecked(bool(getattr(self.canvas, 'visualizer_lineage_mode', True)))
        def _on_lineage_mode(chk):
            with silent_operation("settings_lineage_mode"):
                self.canvas.visualizer_lineage_mode = bool(chk)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.lineage_mode_chk.toggled.connect(_on_lineage_mode)
        adv_form.addRow(self.lineage_mode_chk)

        self.lineage_color_chk = QCheckBox('Color lines by lineage')
        self.lineage_color_chk.setChecked(bool(getattr(self.canvas, 'color_lines_by_lineage', True)))
        def _on_lineage_color(chk):
            with silent_operation("settings_lineage_color" ):
                self.canvas.color_lines_by_lineage = bool(chk)
                _save_if_prefer_app()
                try:
                    self.canvas.refresh_connection_colors()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                _refresh_canvas_items()
        self.lineage_color_chk.toggled.connect(_on_lineage_color)
        adv_form.addRow(self.lineage_color_chk)

        self.auto_layout_chk = QCheckBox('Enable auto layout (beta)')
        self.auto_layout_chk.setChecked(bool(getattr(self.canvas, 'auto_layout_enabled', False)))
        def _on_auto_layout(chk):
            with silent_operation("settings_auto_layout"):
                try:
                    self.canvas.auto_layout_enabled = bool(chk)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                _save_if_prefer_app()
        self.auto_layout_chk.toggled.connect(_on_auto_layout)
        adv_form.addRow(self.auto_layout_chk)

        self.connection_width_spin = QSpinBox(); self.connection_width_spin.setRange(1, 12)
        self.connection_width_spin.setValue(int(getattr(self.canvas, 'connection_line_width', 2)))
        def _on_conn_width(v):
            with silent_operation("settings_connection_width"):
                self.canvas.connection_line_width = int(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.connection_width_spin.valueChanged.connect(_on_conn_width)
        adv_form.addRow('Connection line width:', self.connection_width_spin)

        self.undo_limit_spin = QSpinBox(); self.undo_limit_spin.setRange(10, 10000)
        self.undo_limit_spin.setValue(int(getattr(self.canvas, 'undo_limit', 100)))
        def _on_undo_limit(v):
            with silent_operation("settings_undo_limit"):
                self.canvas.undo_limit = int(v)
                _save_if_prefer_app()
        self.undo_limit_spin.valueChanged.connect(_on_undo_limit)
        adv_form.addRow('Undo history limit:', self.undo_limit_spin)

        self.notes_enabled_chk = QCheckBox('Enable note items')
        self.notes_enabled_chk.setChecked(bool(getattr(self.canvas, 'notes_enabled', False)))
        def _on_notes_enabled(chk):
            with silent_operation("settings_notes_enabled"):
                self.canvas.notes_enabled = bool(chk)
                _save_if_prefer_app()
                try:
                    for it in list(getattr(self.canvas, '_notes_items', [])):
                        it.set_visible(self.canvas.notes_enabled)
                    for nf in list(getattr(self.canvas, '_note_focus_links', [])):
                        nf.setVisible(self.canvas.notes_enabled)
                    for ne in list(getattr(self.canvas, '_note_event_links', [])):
                        ne.setVisible(self.canvas.notes_enabled)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                _refresh_canvas_items()
        self.notes_enabled_chk.toggled.connect(_on_notes_enabled)
        adv_form.addRow(self.notes_enabled_chk)

        self.drag_to_link_chk = QCheckBox('Drag to link mode (prereqs)')
        self.drag_to_link_chk.setChecked(bool(getattr(self.canvas, 'drag_to_link_mode', False)))
        def _on_drag_to_link(chk):
            with silent_operation("settings_drag_to_link"):
                self.canvas.drag_to_link_mode = bool(chk)
                _save_if_prefer_app()
        self.drag_to_link_chk.toggled.connect(_on_drag_to_link)
        adv_form.addRow(self.drag_to_link_chk)

        self.default_focus_color_edit = QLineEdit()
        try:
            cur_def_col = getattr(self.canvas, 'default_focus_color', None)
            if isinstance(cur_def_col, QColor):
                self.default_focus_color_edit.setText(cur_def_col.name(QColor.NameFormat.HexArgb))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        color_pick_btn = QPushButton('Pick…')
        def _pick_default_color():
            try:
                start_col = QColor(self.default_focus_color_edit.text()) if self.default_focus_color_edit.text().strip() else QColor('#cccccc')
            except Exception:
                start_col = QColor('#cccccc')
            col = QColorDialog.getColor(start_col, self, 'Choose default focus color')
            if col.isValid():
                with silent_operation("settings_default_focus_color"):
                    self.canvas.default_focus_color = col
                    self.default_focus_color_edit.setText(col.name(QColor.NameFormat.HexArgb))
                    _save_if_prefer_app()
                    _refresh_canvas_items()
        color_pick_btn.clicked.connect(_pick_default_color)
        col_h = QHBoxLayout(); col_h.addWidget(self.default_focus_color_edit); col_h.addWidget(color_pick_btn)
        adv_form.addRow('Default focus color:', col_h)

        self.note_curve_spin = QDoubleSpinBox(); self.note_curve_spin.setRange(0.1, 5.0); self.note_curve_spin.setSingleStep(0.1)
        try:
            self.note_curve_spin.setValue(float(getattr(self.canvas, 'note_connection_curve_strength', 1.0)))
        except Exception:
            self.note_curve_spin.setValue(1.0)
        def _on_note_curve(v):
            with silent_operation("settings_note_curve"):
                self.canvas.note_connection_curve_strength = float(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.note_curve_spin.valueChanged.connect(_on_note_curve)
        adv_form.addRow('Note connection curve:', self.note_curve_spin)

        self.mutex_display_spin = QDoubleSpinBox(); self.mutex_display_spin.setRange(0.1, 4.0); self.mutex_display_spin.setSingleStep(0.1)
        try:
            self.mutex_display_spin.setValue(float(getattr(self.canvas, 'mutex_icon_display_scale', 1.0)))
        except Exception:
            self.mutex_display_spin.setValue(1.0)
        def _on_mutex_display(v):
            with silent_operation("settings_mutex_display"):
                self.canvas.mutex_icon_display_scale = float(v)
                _save_if_prefer_app()
                _refresh_canvas_items()
        self.mutex_display_spin.valueChanged.connect(_on_mutex_display)
        adv_form.addRow('Mutex icon display scale:', self.mutex_display_spin)

        # --- Keybindings tab ---
        key_tab = QWidget()
        key_layout = QVBoxLayout(key_tab)
        kb_manager = getattr(getattr(self.canvas, 'parent', None), 'keybinds', None)

        info_lbl = QLabel('Manage keyboard shortcuts used throughout the app. Open the editor to change bindings or reset to defaults.')
        info_lbl.setWordWrap(True)
        key_layout.addWidget(info_lbl)

        btn_row = QHBoxLayout()
        open_editor_btn = QPushButton('Open Keybindings Editor…')
        def _open_editor():
            if kb_manager is None:
                QMessageBox.information(self, 'Keybindings', 'Keybindings manager is unavailable.')
                return
            try:
                from _focusGUI import KeybindsEditorDialog  # late import to avoid circular init
            except Exception as exc:
                QMessageBox.warning(self, 'Keybindings', f'Unable to open editor: {exc}')
                return
            dlg = KeybindsEditorDialog(kb_manager, parent=self)
            dlg.exec()
        open_editor_btn.clicked.connect(_open_editor)
        btn_row.addWidget(open_editor_btn)

        reset_btn = QPushButton('Reset All to Defaults')
        def _reset_keybinds():
            if kb_manager is None:
                QMessageBox.information(self, 'Keybindings', 'Keybindings manager is unavailable.')
                return
            for spec in kb_manager.list_commands():
                kb_manager.reset_shortcut(spec.cid)
            QMessageBox.information(self, 'Keybindings', 'All shortcuts reset to defaults. Conflicts resolved by registration order.')
        reset_btn.clicked.connect(_reset_keybinds)
        btn_row.addWidget(reset_btn)

        btn_row.addStretch(1)
        key_layout.addLayout(btn_row)

        # Conflict summary
        self.key_conflict_label = QLabel('')
        self.key_conflict_label.setWordWrap(True)
        key_layout.addWidget(self.key_conflict_label)

        def _refresh_conflicts():
            if kb_manager is None:
                self.key_conflict_label.setText('')
                return
            conflicts = getattr(kb_manager, 'get_conflicts', lambda: {})()
            if not conflicts:
                self.key_conflict_label.setText('No keybinding conflicts detected.')
                return
            parts = []
            for seq, ids in conflicts.items():
                labels = []
                try:
                    spec_map = {s.cid: s.label for s in kb_manager.list_commands()}
                except Exception:
                    spec_map = {}
                for cid in ids:
                    labels.append(spec_map.get(cid, cid))
                parts.append(f"{seq}: {', '.join(labels)}")
            self.key_conflict_label.setText('Conflicts (first listed wins):\n' + '\n'.join(parts))

        try:
            if kb_manager is not None:
                kb_manager.keybinds_changed.connect(_refresh_conflicts)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        _refresh_conflicts()

        tabs.addTab(gen_w, 'General')
        tabs.addTab(appearance_page, 'Appearance')
        tabs.addTab(adv_tab, 'Advanced')
        tabs.addTab(perf_tab, 'Performance')
        tabs.addTab(key_tab, 'Keybindings')

        v.addWidget(tabs)

        # Build a custom button box: Apply (does not close) and Cancel (restore snapshot then close)
        apply_btn = QPushButton('Apply')
        cancel_btn = QPushButton('Cancel')
        def _on_apply():
            try:
                # Persist settings if the parent prefers app-wide settings
                try:
                    if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                        ok = False
                        try:
                            ok = bool(self.canvas.parent.save_settings())
                        except Exception:
                            try:
                                # older save_settings may not return a value
                                self.canvas.parent.save_settings()
                                ok = True
                            except Exception:
                                ok = False
                        # Indicate success by softly highlighting the Apply button; on failure show error dialog
                        try:
                            mw = self.canvas.parent
                        except Exception:
                            mw = None
                        try:
                            if ok:
                                # flash a green border around the Apply button
                                try:
                                    apply_btn.setStyleSheet('border:2px solid #44aa44; border-radius:4px;')
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                try:
                                    QTimer.singleShot(2000, lambda: apply_btn.setStyleSheet(''))
                                except Exception as e:
                                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            else:
                                # Show an error dialog that allows copying the failing path/details
                                try:
                                    dlg = QDialog(self)
                                    dlg.setWindowTitle('Failed to Save Settings')
                                    layout = QVBoxLayout(dlg)
                                    path = '(unknown)'
                                    try:
                                        if mw is not None:
                                            path = str(getattr(mw, 'settings_path', '') or '(unknown)')
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    msg = f"Failed to save settings to:\n{path}\n\nCheck permissions or choose a different folder."
                                    te = QTextEdit(dlg)
                                    te.setReadOnly(True)
                                    te.setPlainText(msg)
                                    layout.addWidget(te)
                                    btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close | QDialogButtonBox.StandardButton.Ok, parent=dlg)
                                    # We'll use Ok as a 'Copy' affordance for quick copying
                                    try:
                                        btns.button(QDialogButtonBox.StandardButton.Ok).setText('Copy')
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    def _on_copy():
                                        try:
                                            QApplication.clipboard().setText(msg)
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    def _on_close():
                                        try:
                                            dlg.accept()
                                        except Exception:
                                            try:
                                                dlg.close()
                                            except Exception as e:
                                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    try:
                                        btns.clicked.connect(lambda b: _on_copy() if btns.standardButton(b) == QDialogButtonBox.StandardButton.Ok else _on_close())
                                    except Exception:
                                        try:
                                            btns.accepted.connect(_on_copy)
                                        except Exception as e:
                                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                                    layout.addWidget(btns)
                                    dlg.exec()
                                except Exception:
                                    try:
                                        QMessageBox.critical(self, 'Save Error', 'Failed to save settings. Check permissions and try again.')
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # refresh nodes
                for n in list(getattr(self.canvas, 'nodes', {}).values()):
                    try:
                        n.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                try:
                    self.canvas.schedule_frame_update()
                except Exception:
                    try:
                        self.canvas.update()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        def _on_cancel():
            try:
                _restore_snapshot()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.reject()
            except Exception:
                try:
                    self.close()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        btn_h = QHBoxLayout()
        btn_h.addStretch(1)
        btn_h.addWidget(apply_btn)
        btn_h.addWidget(cancel_btn)
        apply_btn.clicked.connect(_on_apply)
        cancel_btn.clicked.connect(_on_cancel)
        v.addLayout(btn_h)

        self.setLayout(v)

    # Live update handlers
    def _on_title_size(self, v):
        try:
            self.canvas.focus_title_font_size = int(v)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Auto-persist when preferring app-wide settings
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                # ensure the view is refreshed immediately for live feedback
                try:
                    self.canvas.schedule_frame_update()
                except Exception:
                    self.canvas.update()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_title_outline(self, chk):
        try:
            self.canvas.title_outline_enabled = bool(chk)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_title_outline_th(self, v):
        try:
            self.canvas.title_outline_thickness = int(v)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_title_pill_mode(self, idx):
        try:
            mode = {0: 'default', 1: 'image', 2: 'none'}.get(int(idx), 'default')
            self.canvas.title_pill_mode = mode
            # If switching to Image mode without a path, try to auto-detect or prompt
            if mode == 'image' and not getattr(self.canvas, 'title_pill_image_path', ''):
                try:
                    path = self.canvas.parent.locate_hoi4_pill_image(prompt_if_missing=True)
                    if path:
                        self.canvas.title_pill_image_path = path
                        self.title_pill_path_edit.setText(path)
                        # invalidate cache
                        self.canvas._title_pill_pixmap = None
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_title_pill_image(self):
        try:
            path = self.title_pill_path_edit.text().strip()
            # Validate filename for the expected DDS (allow others but warn)
            if path and os.path.basename(path).lower() != 'focus_can_start_bg.dds':
                QMessageBox.information(self, 'Pill Image', 'Tip: The exact HOI4 pill image is focus_can_start_bg.dds under gfx/interface/focusview/titlebar. Other images may work but could look different.')
            if path != getattr(self.canvas, 'title_pill_image_path', ''):
                self.canvas.title_pill_image_path = path
                self.canvas._title_pill_pixmap = None
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_icon_view_toggle(self, chk):
        try:
            self.canvas.icon_view_mode = bool(chk)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_icon_max(self, v):
        try:
            self.canvas.icon_view_icon_max = int(v)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_icon_bg(self, chk):
        try:
            self.canvas.icon_view_show_background = bool(chk)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_prefer_pillow(self, chk):
        try:
            self.canvas.prefer_pillow_tga = bool(chk)
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_icon_ssaa(self, v):
        try:
            self.canvas.icon_supersample_scale = float(v)
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                if bool(getattr(self.canvas.parent, 'prefer_app_settings', False)) and hasattr(self.canvas.parent, 'save_settings'):
                    self.canvas.parent.save_settings()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                self.canvas.schedule_frame_update()
            except Exception:
                try:
                    self.canvas.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    # ----- General helpers -----
    def _on_tree_id(self):
        try:
            val = self.tree_id_edit.text().strip()
            if val:
                try:
                    self.canvas.parent.tree_id = val
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_country_tag(self):
        try:
            val = self.country_edit.text().strip()
            if val:
                try:
                    self.canvas.parent.country_tag = val
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _change_settings_path(self):
        try:
            fn, _ = QFileDialog.getSaveFileName(self, 'Choose settings file', self.settings_path_label.text() or os.getcwd(), 'JSON (*.json)')
            if fn:
                try:
                    self.canvas.parent.settings_path = fn
                    # show obfuscated/short form and provide full path in tooltip
                    try:
                        self.settings_path_label.setText(self._obfuscate_user_in_path(fn))
                        self.settings_path_label.setToolTip(fn)
                    except Exception:
                        set_widget_path_display(self.settings_path_label, fn)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _change_database_path(self):
        try:
            fn, _ = QFileDialog.getSaveFileName(self, 'Choose database file', self.database_path_label.text() or os.getcwd(), 'JSON (*.json)')
            if fn:
                try:
                    self.canvas.parent.database_path = fn
                    try:
                        self.database_path_label.setText(self._obfuscate_user_in_path(fn))
                        self.database_path_label.setToolTip(fn)
                    except Exception:
                        set_widget_path_display(self.database_path_label, fn)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _do_load_database(self):
        try:
            self.canvas.parent.load_database()
            QMessageBox.information(self, 'Database', 'Database loaded.')
        except Exception as e:
            show_error(self, 'Database', 'Failed to load database.', exc=e)

    def _do_save_database(self):
        try:
            self.canvas.parent.save_database()
            QMessageBox.information(self, 'Database', 'Database saved.')
        except Exception as e:
            show_error(self, 'Database', 'Failed to save database.', exc=e)

    def _open_icon_library(self):
        try:
            dlg = IconLibraryDialog(self.canvas.parent.icon_library, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted and getattr(dlg, 'selected', None):
                sel = str(getattr(dlg, 'selected', '') or '')
                QMessageBox.information(self, 'Icon Library', f"Selected icon: {obfuscate_text(sel)}")
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _reset_canvas_defaults(self):
        try:
            # Reset only visual settings to safe defaults
            self.canvas.icon_view_mode = False
            self.canvas.icon_view_icon_max = 120
            self.canvas.icon_view_show_background = False
            self.canvas.title_outline_enabled = True
            self.canvas.title_outline_thickness = 1
            self.canvas.title_pill_mode = 'default'
            self.canvas.title_pill_image_path = ''
            self.canvas._title_pill_pixmap = None
            self.canvas.focus_title_font_size = 14
            self.canvas.icon_supersample_scale = 1.0
            self.canvas.set_grid_visible(True)
            self.canvas.frames_enabled = False
            self.canvas.refresh_connection_colors()
            for n in list(self.canvas.nodes.values()):
                try:
                    n.update()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            QMessageBox.information(self, 'Reset', 'Canvas settings reset to defaults.')
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
