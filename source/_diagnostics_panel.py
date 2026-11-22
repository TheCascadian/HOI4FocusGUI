"""Dockable diagnostics panel.

Presentation only. Every rule about what is wrong with a focus tree lives in
:mod:`_validation`; this module decides how findings are listed, filtered,
and turned into a navigation request. Keeping the split sharp is what lets
the same rules run in a headless script and in CI.

Two behaviours are worth calling out because they are the reason this is a
panel rather than a dialog:

**It never blocks the event loop.** A tree with thousands of focuses takes
long enough to validate that doing it in one call would freeze the window.
:class:`DiagnosticsPanel` drives :func:`~_validation.iter_validate_focus_tree`
through a zero-interval timer, consuming a bounded slice of the diagnostic
stream per tick and returning to Qt in between.

**It never accumulates stale results.** Each run gets a token; rows carrying
an older token are discarded, and starting a run clears the view and drops
the previous generator. Re-validating the same large tree a hundred times
leaves exactly one run's worth of rows behind.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterator, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from _validation import (
    Diagnostic,
    Severity,
    ValidationContext,
    ValidationResult,
    iter_validate_focus_tree,
)

logger = logging.getLogger(__name__)

__all__ = ["DiagnosticsPanel", "SEVERITY_COLORS"]


#: How each severity reads at a glance. Errors are the only ones that block
#: an export, so they are the only ones in alarm red.
SEVERITY_COLORS: Dict[Severity, QColor] = {
    Severity.ERROR: QColor(220, 76, 70),
    Severity.WARNING: QColor(220, 160, 60),
    Severity.INFO: QColor(120, 160, 210),
}

SEVERITY_LABELS: Dict[Severity, str] = {
    Severity.ERROR: "Error",
    Severity.WARNING: "Warning",
    Severity.INFO: "Info",
}

#: Diagnostics pulled from the stream per timer tick. Large enough that a
#: clean tree finishes in one or two ticks, small enough that the window
#: stays responsive while a pathological tree grinds through thousands.
_CHUNK_SIZE = 200

#: Role holding the Diagnostic on its row, so activation does not have to
#: reconstruct anything from the displayed text.
_DIAGNOSTIC_ROLE = Qt.ItemDataRole.UserRole


class DiagnosticsPanel(QDockWidget):
    """Lists validation findings and turns a click into a navigation request.

    The panel does not know how to select a focus -- it emits
    :attr:`diagnostic_activated` and lets the editor decide. That keeps the
    panel testable without a canvas and keeps canvas details out of it.
    """

    #: Emitted when the user activates a row. Carries the Diagnostic.
    diagnostic_activated = pyqtSignal(object)
    #: Emitted when a run completes. Carries the ValidationResult.
    validation_finished = pyqtSignal(object)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        tree_provider: Optional[Callable[[], Any]] = None,
        context_provider: Optional[Callable[[], ValidationContext]] = None,
    ):
        super().__init__("Diagnostics", parent)
        self.setObjectName("DiagnosticsDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )

        #: Callables supplied by the editor so the panel can re-validate on
        #: demand without holding a reference to the tree itself (which would
        #: go stale the moment the user opens another project).
        self._tree_provider = tree_provider
        self._context_provider = context_provider

        # --- in-flight run state -------------------------------------------
        self._stream: Optional[Iterator[Diagnostic]] = None
        self._collected: List[Diagnostic] = []
        self._run_token = 0
        self._last_result: Optional[ValidationResult] = None

        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._consume_chunk)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)

        controls = QHBoxLayout()

        self.revalidate_button = QPushButton("Validate", container)
        self.revalidate_button.setToolTip("Re-run validation on the current focus tree")
        self.revalidate_button.clicked.connect(self.revalidate)
        controls.addWidget(self.revalidate_button)

        # Severity filters. Errors stay visible by default because they are
        # the ones that stop an export; the others are opt-out noise control.
        self.show_errors = QCheckBox("Errors", container)
        self.show_warnings = QCheckBox("Warnings", container)
        self.show_infos = QCheckBox("Info", container)
        for box in (self.show_errors, self.show_warnings, self.show_infos):
            box.setChecked(True)
            box.stateChanged.connect(self._apply_filters)
            controls.addWidget(box)

        controls.addStretch(1)

        self.summary_label = QLabel("Not validated yet.", container)
        controls.addWidget(self.summary_label)

        layout.addLayout(controls)

        self.tree = QTreeWidget(container)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Severity", "Code", "Focus", "Message"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemActivated.connect(self._on_item_activated)

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        layout.addWidget(self.tree)
        self.setWidget(container)

    # ------------------------------------------------------------------
    # Running validation
    # ------------------------------------------------------------------

    def revalidate(self) -> None:
        """Validate the tree currently supplied by the tree provider."""
        if self._tree_provider is None:
            logger.debug("DiagnosticsPanel.revalidate called with no tree provider")
            return
        context = self._context_provider() if self._context_provider else None
        self.start_validation(self._tree_provider(), context)

    def start_validation(
        self,
        tree: Any,
        context: Optional[ValidationContext] = None,
    ) -> None:
        """Begin an incremental validation run, cancelling any run in flight.

        Returns immediately. Listen for :attr:`validation_finished` to learn
        the outcome, or call :meth:`validate_now` when you need it inline.
        """
        self._cancel_run()
        self._run_token += 1
        self._collected = []
        self._last_result = None
        self.tree.clear()
        self.summary_label.setText("Validating...")
        self._stream = iter_validate_focus_tree(tree, context)
        self._timer.start()

    def validate_now(
        self,
        tree: Any,
        context: Optional[ValidationContext] = None,
    ) -> ValidationResult:
        """Validate synchronously and return the result.

        Used by the export path, where there is nothing useful for the user
        to do until the answer is known, and by tests that do not want to
        spin an event loop.
        """
        self.start_validation(tree, context)
        while self._stream is not None:
            self._consume_chunk()
        return self._last_result or ValidationResult()

    def _cancel_run(self) -> None:
        """Drop any in-flight run so its rows can never land in a later one."""
        self._timer.stop()
        self._stream = None

    def _consume_chunk(self) -> None:
        """Pull one bounded slice off the diagnostic stream."""
        if self._stream is None:
            self._timer.stop()
            return

        token = self._run_token
        produced: List[Diagnostic] = []
        finished = False

        for _ in range(_CHUNK_SIZE):
            try:
                produced.append(next(self._stream))
            except StopIteration:
                finished = True
                break
            except Exception:
                # A rule raising is a defect in the validator, not something
                # the user can act on. Log it with the traceback and end the
                # run rather than leaving the panel spinning forever.
                logger.exception("Validation rule raised; aborting run")
                finished = True
                break

        # A newer run started while this chunk was being produced: throw the
        # work away rather than mixing two runs' findings.
        if token != self._run_token:
            return

        self._collected.extend(produced)

        if finished:
            self._finish_run()

    def _finish_run(self) -> None:
        """Sort, render, and publish the completed run."""
        self._timer.stop()
        self._stream = None

        result = ValidationResult(
            diagnostics=sorted(self._collected, key=lambda d: d.sort_key)
        )
        self._last_result = result
        self._collected = []

        self._populate(result)
        self._update_summary(result)
        self.validation_finished.emit(result)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _populate(self, result: ValidationResult) -> None:
        self.tree.clear()
        items: List[QTreeWidgetItem] = []
        for diagnostic in result.diagnostics:
            item = QTreeWidgetItem(
                [
                    SEVERITY_LABELS[diagnostic.severity],
                    diagnostic.code,
                    diagnostic.object_id or "",
                    diagnostic.message,
                ]
            )
            item.setData(0, _DIAGNOSTIC_ROLE, diagnostic)
            colour = QBrush(SEVERITY_COLORS[diagnostic.severity])
            item.setForeground(0, colour)
            item.setToolTip(3, self._tooltip_for(diagnostic))
            items.append(item)

        self.tree.addTopLevelItems(items)
        self._apply_filters()

    @staticmethod
    def _tooltip_for(diagnostic: Diagnostic) -> str:
        parts = [diagnostic.message]
        if diagnostic.field_path:
            parts.append(f"Field: {diagnostic.field_path}")
        if diagnostic.location:
            parts.append(f"Source: {diagnostic.location}")
        if diagnostic.suggestion:
            parts.append(f"Suggestion: {diagnostic.suggestion}")
        return "\n".join(parts)

    def _update_summary(self, result: ValidationResult) -> None:
        counts = result.counts()
        if not result.diagnostics:
            self.summary_label.setText("No problems found.")
            return
        self.summary_label.setText(
            f"{counts[Severity.ERROR]} errors, "
            f"{counts[Severity.WARNING]} warnings, "
            f"{counts[Severity.INFO]} info"
        )

    def _apply_filters(self) -> None:
        enabled = {
            Severity.ERROR: self.show_errors.isChecked(),
            Severity.WARNING: self.show_warnings.isChecked(),
            Severity.INFO: self.show_infos.isChecked(),
        }
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            diagnostic = item.data(0, _DIAGNOSTIC_ROLE)
            item.setHidden(not enabled.get(diagnostic.severity, True))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        diagnostic = self.selected_diagnostic()
        if diagnostic is not None:
            self.diagnostic_activated.emit(diagnostic)

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        diagnostic = item.data(0, _DIAGNOSTIC_ROLE)
        if diagnostic is not None:
            self.diagnostic_activated.emit(diagnostic)

    def selected_diagnostic(self) -> Optional[Diagnostic]:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, _DIAGNOSTIC_ROLE)

    def select_row(self, index: int) -> None:
        """Select a row by position. Primarily for tests and keyboard flows."""
        item = self.tree.topLevelItem(index)
        if item is not None:
            self.tree.setCurrentItem(item)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def last_result(self) -> Optional[ValidationResult]:
        """The most recent completed run, or None if none has finished."""
        return self._last_result

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def visible_row_count(self) -> int:
        return sum(
            0 if self.tree.topLevelItem(i).isHidden() else 1
            for i in range(self.tree.topLevelItemCount())
        )

    def row_count(self) -> int:
        return self.tree.topLevelItemCount()

    def show_result(self, result: ValidationResult) -> None:
        """Display an already-computed result, e.g. one produced by export.

        Lets the pre-export check reuse the panel instead of raising a modal
        dialog listing the problems.
        """
        self._cancel_run()
        self._run_token += 1
        self._collected = []
        self._last_result = result
        self._populate(result)
        self._update_summary(result)
