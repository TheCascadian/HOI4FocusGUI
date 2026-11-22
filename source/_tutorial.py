"""
Interactive, non-modal, compact tutorial coach for HOI4 Focus Tree Generator.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _SpotlightOverlay(QWidget):
    """Click-through overlay that draws a subtle highlight box around a target widget."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._target_rect: Optional[QRect] = None
        self._pulse_phase = 0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._on_pulse)
        self._pulse_timer.start(80)
        self.hide()

    def set_target_widget(self, widget: Optional[QWidget]) -> None:
        if widget is None or not widget.isVisible():
            self._target_rect = None
            self.hide()
            return
        parent = self.parentWidget()
        if parent is None:
            self._target_rect = None
            self.hide()
            return
        self.setGeometry(parent.rect())
        top_left = widget.mapTo(parent, QPoint(0, 0))
        self._target_rect = QRect(top_left, widget.size()).adjusted(-2, -2, 2, 2)
        self.raise_()
        self.show()
        self.update()

    def _on_pulse(self) -> None:
        if self.isVisible() and self._target_rect is not None:
            self._pulse_phase = (self._pulse_phase + 1) % 20
            self.update()

    def paintEvent(self, event) -> None:
        if self._target_rect is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = 140 + abs(10 - self._pulse_phase) * 10
        painter.setPen(QPen(QColor(74, 158, 255, alpha), 2))
        painter.setBrush(QColor(74, 158, 255, 14))
        painter.drawRoundedRect(self._target_rect, 3, 3)

    def closeEvent(self, event) -> None:
        self._pulse_timer.stop()
        super().closeEvent(event)


class InteractiveTutorialCoach(QFrame):
    """Compact, non-modal floating tutorial card matching the native dark UI."""

    def __init__(self, main_window: Any):
        super().__init__(main_window)
        self.main_window = main_window
        self.setObjectName("InteractiveTutorialCoach")
        self.setFixedWidth(330)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            """
            QFrame#InteractiveTutorialCoach {
                background-color: #232325;
                border: 1px solid #3c3c40;
                border-radius: 4px;
            }
            QLabel#TutorialStepBadge {
                color: #4a9eff;
                font-weight: bold;
                font-size: 11px;
            }
            QLabel#TutorialDragHint {
                color: #77777b;
                font-size: 10px;
            }
            QLabel#TutorialTitle {
                color: #ececee;
                font-size: 13px;
                font-weight: bold;
            }
            QLabel#TutorialBody {
                color: #c8c8cc;
                font-size: 12px;
            }
            QProgressBar#TutorialProgress {
                background-color: #1a1a1c;
                border: none;
                border-radius: 1px;
                height: 3px;
            }
            QProgressBar#TutorialProgress::chunk {
                background-color: #4a9eff;
                border-radius: 1px;
            }
            """
        )

        self._overlay = _SpotlightOverlay(main_window)
        self._drag_offset: Optional[QPoint] = None
        self._step_idx = 0
        self._baseline: Dict[str, Any] = {}
        self._completed_steps: set[int] = set()

        self._build_ui()
        self._steps = self._create_steps()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._check_live_mission)
        self._poll_timer.start(250)

        self._render_step()
        self._position_card()
        self.show()
        self.raise_()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        self._step_badge = QLabel()
        self._step_badge.setObjectName("TutorialStepBadge")
        hdr.addWidget(self._step_badge)
        hdr.addStretch(1)
        hint_drag = QLabel("Drag to move")
        hint_drag.setObjectName("TutorialDragHint")
        hdr.addWidget(hint_drag)
        root.addLayout(hdr)

        self._progress = QProgressBar()
        self._progress.setObjectName("TutorialProgress")
        self._progress.setTextVisible(False)
        root.addWidget(self._progress)

        self._title_lbl = QLabel()
        self._title_lbl.setObjectName("TutorialTitle")
        self._title_lbl.setWordWrap(True)
        root.addWidget(self._title_lbl)

        self._body_lbl = QLabel()
        self._body_lbl.setObjectName("TutorialBody")
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self._body_lbl)

        # Compact inline mission strip
        self._mission_frame = QFrame()
        self._mission_frame.setObjectName("MissionBox")
        m_lay = QVBoxLayout(self._mission_frame)
        m_lay.setContentsMargins(8, 6, 8, 6)
        m_lay.setSpacing(5)
        self._mission_status = QLabel()
        self._mission_status.setWordWrap(True)
        self._mission_status.setTextFormat(Qt.TextFormat.RichText)
        self._mission_status.setStyleSheet("font-size: 11px; color: #d0d0d4;")
        m_lay.addWidget(self._mission_status)

        self._action_btn = QPushButton()
        self._action_btn.clicked.connect(self._on_action_btn)
        m_lay.addWidget(self._action_btn)
        root.addWidget(self._mission_frame)

        # Footer
        self._startup_cb = QCheckBox("Show on startup")
        self._startup_cb.setChecked(bool(getattr(self.main_window, "show_tutorial_on_startup", True)))
        self._startup_cb.toggled.connect(self._on_startup_toggled)
        root.addWidget(self._startup_cb)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        self._skip_btn = QPushButton("Close")
        self._skip_btn.clicked.connect(self.close)
        self._back_btn = QPushButton("Back")
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn = QPushButton("Next")
        self._next_btn.setDefault(True)
        self._next_btn.clicked.connect(self._go_next)
        nav.addWidget(self._skip_btn)
        nav.addStretch(1)
        nav.addWidget(self._back_btn)
        nav.addWidget(self._next_btn)
        root.addLayout(nav)

    def _create_steps(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Welcome to HOI4 Focus GUI",
                "body": "Place focus boxes and draw arrows to build a focus tree—no Paradox scripting required.",
                "target": None,
                "mission_text": "Optional: Load a 3-focus starter tree to explore.",
                "done_text": "Starter tree loaded.",
                "action_label": "Load 3-Focus Example",
                "action_cb": self._load_mini_example,
                "check_cb": None,
            },
            {
                "title": "Tree ID & Country Tag",
                "body": "Set a <b>Tree ID</b> (no spaces) and 3-letter <b>Country Tag</b> (e.g. <code>USA</code>, <code>GER</code>) in the top bar.",
                "target": self._get_country_or_tree_widget,
                "mission_text": "Edit either field in the toolbar.",
                "done_text": "Tree identity updated.",
                "action_label": "Use Sample (my_focus_tree / USA)",
                "action_cb": self._set_sample_tag,
                "check_cb": lambda b: (
                    getattr(self.main_window, "tree_id", "") != b.get("tree_id")
                    or getattr(self.main_window, "country_tag", "") != b.get("country_tag")
                ),
            },
            {
                "title": "Add & Edit Focuses",
                "body": "Right-click the grid → <b>Add Focus</b> (<code>Ctrl+Shift+F</code>). Double-click any focus to edit its name, days, or rewards.",
                "target": self._get_canvas_widget,
                "mission_text": "Add at least 1 focus to the grid.",
                "done_text": "Focus added!",
                "action_label": "Add a Focus",
                "action_cb": self._add_sample_focus,
                "check_cb": lambda b: len(getattr(self.main_window, "focuses", [])) > b.get("focus_count", 0),
            },
            {
                "title": "Connect & Exclusive Paths",
                "body": "Toggle <b>Connection Mode</b>, then click <b>Parent → Child</b> to link prerequisites. Use <b>Advanced → Pick…</b> for mutually exclusive choices.",
                "target": self._get_canvas_widget,
                "mission_text": "Connect two focuses with an arrow.",
                "done_text": "Focuses connected!",
                "action_label": "Create Demo Connection",
                "action_cb": self._create_demo_connection,
                "check_cb": lambda b: len(getattr(getattr(self.main_window, "canvas", None), "connections", [])) > b.get("conn_count", 0),
            },
            {
                "title": "Navigate & Auto-Generate",
                "body": "<b>Middle-drag</b> to pan, <b>Wheel</b> to zoom, <code>Ctrl+Z</code> to undo. Press <code>Ctrl+G</code> to generate a full tree instantly.",
                "target": self._get_canvas_widget,
                "mission_text": None,
                "done_text": "",
                "action_label": "Open Random Generator (Ctrl+G)",
                "action_cb": self._open_generator_safe,
                "check_cb": None,
            },
            {
                "title": "Export to HOI4",
                "body": "Open <b>File → Open Export Panel</b> (<code>Ctrl+Shift+E</code>) to export your <code>.txt</code> focus tree and <code>.yml</code> localisation files. Reopen anytime via <b>Help → Starter Tutorial…</b>.",
                "target": None,
                "mission_text": None,
                "done_text": "",
                "action_label": None,
                "action_cb": None,
                "check_cb": None,
            },
        ]

    def _get_country_or_tree_widget(self) -> Optional[QWidget]:
        return getattr(self.main_window, "country_edit", None) or getattr(self.main_window, "tree_id_edit", None)

    def _get_canvas_widget(self) -> Optional[QWidget]:
        canvas = getattr(self.main_window, "canvas", None)
        if canvas is not None and hasattr(canvas, "views"):
            views = canvas.views()
            if views:
                return views[0]
        return None

    def _load_mini_example(self) -> None:
        if not hasattr(self.main_window, "load_project_from_dict"):
            return
        sample = {
            "version": "1.0.12",
            "tree_id": "starter_example",
            "country_tag": "USA",
            "focuses": [
                {
                    "id": "starter_industry",
                    "name": "Industrial Awakening",
                    "x": 2,
                    "y": 0,
                    "cost": 10,
                    "prerequisites": [],
                    "completion_reward": "add_political_power = 50",
                },
                {
                    "id": "starter_civilian",
                    "name": "Civilian Expansion",
                    "x": 1,
                    "y": 1,
                    "cost": 10,
                    "prerequisites": ["starter_industry"],
                    "mutually_exclusive": ["starter_military"],
                },
                {
                    "id": "starter_military",
                    "name": "Rearmament Drive",
                    "x": 3,
                    "y": 1,
                    "cost": 10,
                    "prerequisites": ["starter_industry"],
                    "mutually_exclusive": ["starter_civilian"],
                },
            ],
        }
        self.main_window.load_project_from_dict(sample)
        self._completed_steps.add(self._step_idx)
        self._render_step()

    def _set_sample_tag(self) -> None:
        if hasattr(self.main_window, "tree_id_edit"):
            self.main_window.tree_id_edit.setText("my_focus_tree")
            self.main_window.tree_id = "my_focus_tree"
        if hasattr(self.main_window, "country_edit"):
            self.main_window.country_edit.setText("USA")
            self.main_window.country_tag = "USA"

    def _add_sample_focus(self) -> None:
        from _dataStructs import Focus

        existing = {f.id for f in getattr(self.main_window, "focuses", [])}
        idx = len(existing) + 1
        fid = f"tutorial_focus_{idx}"
        while fid in existing:
            idx += 1
            fid = f"tutorial_focus_{idx}"
        fx = Focus(id=fid, name=f"New Focus {idx}", x=(idx % 5) * 2, y=idx // 5, cost=10)
        self.main_window.focuses.append(fx)
        if hasattr(self.main_window, "canvas") and hasattr(self.main_window.canvas, "add_focus_node"):
            self.main_window.canvas.add_focus_node(fx)

    def _create_demo_connection(self) -> None:
        focuses = getattr(self.main_window, "focuses", [])
        if len(focuses) < 2:
            self._add_sample_focus()
            if len(focuses) < 2:
                self._add_sample_focus()
        if len(focuses) >= 2:
            parent_f = focuses[-2]
            child_f = focuses[-1]
            if parent_f.id not in child_f.prerequisites:
                child_f.prerequisites.append(parent_f.id)
            if hasattr(self.main_window, "canvas") and hasattr(self.main_window.canvas, "create_connection"):
                self.main_window.canvas.create_connection(parent_f.id, child_f.id)

    def _open_generator_safe(self) -> None:
        for attr in ("show_generate_dialog", "open_generator_dialog", "show_generator_dialog"):
            fn = getattr(self.main_window, attr, None)
            if callable(fn):
                fn()
                return

    def _capture_baseline(self) -> None:
        self._baseline = {
            "tree_id": getattr(self.main_window, "tree_id", ""),
            "country_tag": getattr(self.main_window, "country_tag", ""),
            "focus_count": len(getattr(self.main_window, "focuses", [])),
            "conn_count": len(getattr(getattr(self.main_window, "canvas", None), "connections", [])),
        }

    def _check_live_mission(self) -> None:
        if self._step_idx in self._completed_steps:
            return
        step = self._steps[self._step_idx]
        check_cb = step.get("check_cb")
        if callable(check_cb):
            try:
                if check_cb(self._baseline):
                    self._completed_steps.add(self._step_idx)
                    self._update_mission_box()
            except Exception:
                pass

    def _update_mission_box(self) -> None:
        step = self._steps[self._step_idx]
        mission_text = step.get("mission_text")
        action_label = step.get("action_label")
        is_done = self._step_idx in self._completed_steps

        if not mission_text and not action_label:
            self._mission_frame.hide()
            return

        self._mission_frame.show()
        if is_done:
            self._mission_frame.setStyleSheet(
                "QFrame#MissionBox { background-color: #1b2a22; border: 1px solid #357a52; border-radius: 3px; }"
            )
            self._mission_status.setText(f"<b style='color:#6ee7b7;'>✓ {step.get('done_text') or 'Done!'}</b>")
        else:
            self._mission_frame.setStyleSheet(
                "QFrame#MissionBox { background-color: #1c1c1e; border: 1px solid #343438; border-radius: 3px; }"
            )
            if mission_text:
                self._mission_status.setText(f"▸ {mission_text}")
                self._mission_status.show()
            else:
                self._mission_status.hide()

        if action_label:
            self._action_btn.setText(action_label)
            self._action_btn.show()
        else:
            self._action_btn.hide()

    def _render_step(self) -> None:
        total = len(self._steps)
        step = self._steps[self._step_idx]
        self._step_badge.setText(f"Step {self._step_idx + 1} of {total}")
        self._progress.setMaximum(total)
        self._progress.setValue(self._step_idx + 1)
        self._title_lbl.setText(step["title"])
        self._body_lbl.setText(step["body"])

        self._capture_baseline()
        self._update_mission_box()

        self._back_btn.setEnabled(self._step_idx > 0)
        self._next_btn.setText("Finish" if self._step_idx == total - 1 else "Next")

        target_fn = step.get("target")
        target_widget = target_fn() if callable(target_fn) else None
        self._overlay.set_target_widget(target_widget)
        self.raise_()
        self.adjustSize()

    def _on_action_btn(self) -> None:
        cb = self._steps[self._step_idx].get("action_cb")
        if callable(cb):
            cb()

    def _go_back(self) -> None:
        if self._step_idx > 0:
            self._step_idx -= 1
            self._render_step()

    def _go_next(self) -> None:
        if self._step_idx < len(self._steps) - 1:
            self._step_idx += 1
            self._render_step()
        else:
            self.close()

    def _on_startup_toggled(self, checked: bool) -> None:
        self.main_window.show_tutorial_on_startup = bool(checked)
        if hasattr(self.main_window, "save_settings"):
            try:
                self.main_window.save_settings()
            except Exception:
                pass

    def _position_card(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        margin = 16
        x = max(margin, parent.width() - self.width() - margin)
        y = max(margin, parent.height() - self.height() - margin - 24)
        self.move(x, y)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            parent = self.parentWidget()
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            if parent is not None:
                local_pos = parent.mapFromGlobal(new_pos)
                max_x = max(0, parent.width() - self.width())
                max_y = max(0, parent.height() - self.height())
                local_pos.setX(max(0, min(max_x, local_pos.x())))
                local_pos.setY(max(0, min(max_y, local_pos.y())))
                self.move(local_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:
        self._poll_timer.stop()
        self._overlay.close()
        self._overlay.deleteLater()
        super().closeEvent(event)
