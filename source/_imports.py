"""Common imports for the project (compatibility facade).

STATUS: legacy. This module is retained only so that modules which have not yet
been converted to explicit imports keep working. Do not add new dependents, and
do not import it from ``_dataStructs``, ``_utils``, ``_exporter``, or
``_import_pipeline`` — those modules now use explicit imports and importing this
facade from them would re-introduce the import cycle it used to paper over.

This module acts as a centralized, well-documented imports facade for
shared runtime symbols used across the project. It consolidates repeated
imports to reduce overhead and ensure consistency across all modules.

BENEFITS:
    - Reduces import statement overhead (each import is an I/O operation)
    - Prevents redundant module loading across files
    - Provides a single source of truth for commonly used symbols
    - Simplifies file headers and reduces cognitive load
    - Makes refactoring import patterns project-wide trivial

USAGE:
    # Import specific symbols (recommended for most files)
    from _imports import (
        os, json, Path,
        List, Optional, Dict,
        QApplication, QMainWindow, Qt, QPointF,
        Focus, Event, show_error,
    )

    # Or import everything for interactive/script use
    from _imports import *

CATEGORIES EXPORTED:
    - Standard library: os, sys, json, pathlib, datetime, etc.
    - Typing: List, Optional, Dict, Any, TYPE_CHECKING, etc.
    - PyQt6.QtWidgets: QApplication, QMainWindow, QDialog, etc.
    - PyQt6.QtCore: Qt, QPointF, QRectF, QTimer, etc.
    - PyQt6.QtGui: QPainter, QColor, QPen, QBrush, etc.
    - Third-party: PIL.Image, numpy (optional)
    - Project: Focus, Event, CommandSpec, show_error, etc.
"""

# region File (auto-generated)
# endregion

import logging

logger = logging.getLogger(__name__)

# =============================================================================
# Standard Library
# =============================================================================
import datetime
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from contextlib import suppress
from functools import wraps
from pathlib import Path

import hashlib
import shutil
import subprocess
import tempfile
import threading
import traceback
import uuid
import weakref

# =============================================================================
# Typing
# =============================================================================
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    TYPE_CHECKING,
    Union,
)

# =============================================================================
# PyQt6 - Widgets
# =============================================================================
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsProxyWidget,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneContextMenuEvent,
    QGraphicsSceneMouseEvent,
    QGraphicsSimpleTextItem,
    QGraphicsTextItem,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpacerItem,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyleFactory,
    QTabBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

# =============================================================================
# PyQt6 - Core
# =============================================================================
from PyQt6.QtCore import (
    pyqtSignal,
    pyqtSlot,
    QBuffer,
    QByteArray,
    QCoreApplication,
    QDate,
    QDateTime,
    QEvent,
    QIODevice,
    QLine,
    QLineF,
    QLocale,
    QMimeData,
    QModelIndex,
    QMutex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QRegularExpression,
    QSettings,
    QSize,
    QSizeF,
    Qt,
    QThread,
    QTime,
    QTimer,
    QUrl,
    QWaitCondition,
)

# =============================================================================
# PyQt6 - GUI
# =============================================================================
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QClipboard,
    QCloseEvent,
    QColor,
    QConicalGradient,
    QContextMenuEvent,
    QCursor,
    QDesktopServices,
    QDoubleValidator,
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QEnterEvent,
    QFocusEvent,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QGradient,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QImage,
    QImageReader,
    QImageWriter,
    QIntValidator,
    QKeyEvent,
    QKeySequence,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
    QPolygon,
    QPolygonF,
    QRadialGradient,
    QRegion,
    QResizeEvent,
    QScreen,
    QShortcut,
    QShowEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTransform,
    QUndoCommand,
    QValidator,
    QWheelEvent,
)

# =============================================================================
# Optional Third-Party
# =============================================================================
# Pillow and numpy are genuinely optional at runtime; consumers test these for
# ``None`` before use. Availability is probed with ``find_spec`` so that a real
# ImportError raised *inside* an installed package still propagates instead of
# being silently downgraded to "not installed".
import importlib.util

if importlib.util.find_spec("PIL") is not None:
    from PIL import Image
else:
    Image = None  # type: ignore

if importlib.util.find_spec("numpy") is not None:
    import numpy as np
else:
    np = None  # type: ignore

# =============================================================================
# Project Data Structures
# =============================================================================
# ``_dataStructs`` depends only on the standard library and ``error_handler``,
# so this is an unconditional import: a failure here is a real error and must
# not be masked by a placeholder ``None``.
from _dataStructs import Focus, Event, CommandSpec

# =============================================================================
# Utility Helpers
# =============================================================================
# ``_utils`` depends on ``_dataStructs`` + PyQt6 and never imports this facade,
# so this too is a plain import with no fallbacks.
from _utils import (
    clone_focus_pure,
    draw_outlined_text,
    format_project_file_size,
    obfuscate_path,
    obfuscate_text,
    obfuscate_user_in_path,
    pixmap_from_file_via_pillow,
    safe_qt_call,
    safe_ui_operation,
    set_widget_path_display,
    show_error,
)

# =============================================================================
# Converters (resolved lazily)
# =============================================================================
# ``_txt_converter`` still imports this facade, so importing it here at module
# scope would re-create a cycle. Resolve it on first attribute access instead:
# ``from _imports import convert_txt_to_project_dict`` keeps working, and a
# genuinely broken ``_txt_converter`` raises instead of yielding ``None``.
if TYPE_CHECKING:
    from _txt_converter import convert_txt_to_project_dict


def __getattr__(name: str):
    """Resolve lazily-exported names (PEP 562)."""
    if name == "convert_txt_to_project_dict":
        from _txt_converter import convert_txt_to_project_dict as _converter

        globals()[name] = _converter
        return _converter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# =============================================================================
# Public Export Surface
# =============================================================================
__all__ = [
    # Standard library re-exports
    "datetime", "json", "math", "os", "re", "sys", "time",
    "defaultdict", "suppress", "wraps", "Path",
    "hashlib", "shutil", "subprocess", "tempfile", "threading",
    "traceback", "uuid", "weakref",
    "logger",

    # Typing
    "Any", "Callable", "Dict", "Iterable", "Iterator", "List",
    "Optional", "Sequence", "Set", "Tuple", "TYPE_CHECKING", "Union",

    # PyQt6 Widgets
    "QAbstractItemView", "QAbstractScrollArea", "QApplication", "QCheckBox",
    "QColorDialog", "QComboBox", "QCompleter", "QDialog", "QDialogButtonBox",
    "QDockWidget", "QDoubleSpinBox", "QFileDialog", "QFormLayout", "QFrame",
    "QGraphicsDropShadowEffect", "QGraphicsEllipseItem", "QGraphicsItem",
    "QGraphicsLineItem", "QGraphicsObject", "QGraphicsPathItem",
    "QGraphicsPixmapItem", "QGraphicsPolygonItem", "QGraphicsProxyWidget",
    "QGraphicsRectItem", "QGraphicsScene", "QGraphicsSceneContextMenuEvent",
    "QGraphicsSceneMouseEvent", "QGraphicsSimpleTextItem", "QGraphicsTextItem",
    "QGraphicsView", "QGridLayout", "QGroupBox", "QHBoxLayout", "QHeaderView",
    "QInputDialog", "QLabel", "QLineEdit", "QListWidget", "QListWidgetItem",
    "QMainWindow", "QMenu", "QMenuBar", "QMessageBox", "QPlainTextEdit",
    "QProgressBar", "QPushButton", "QScrollArea", "QSizePolicy", "QSlider",
    "QSpacerItem", "QSpinBox", "QSplitter", "QStackedWidget", "QStatusBar",
    "QStyle", "QStyleFactory", "QTabBar", "QTabWidget", "QTableWidget",
    "QTableWidgetItem", "QTextEdit", "QToolBar", "QToolButton", "QToolTip",
    "QTreeWidget", "QTreeWidgetItem", "QVBoxLayout",
    "QWidget", "QWidgetAction",

    # PyQt6 Core
    "pyqtSignal", "pyqtSlot", "QBuffer", "QByteArray", "QCoreApplication",
    "QDate", "QDateTime", "QEvent", "QIODevice", "QLine", "QLineF", "QLocale",
    "QMimeData", "QModelIndex", "QMutex", "QObject", "QPoint", "QPointF",
    "QRect", "QRectF", "QRegularExpression", "QSettings", "QSize", "QSizeF",
    "Qt", "QThread", "QTime", "QTimer", "QUrl", "QWaitCondition",

    # PyQt6 GUI
    "QAction", "QActionGroup", "QBrush", "QClipboard", "QCloseEvent", "QColor",
    "QConicalGradient", "QContextMenuEvent", "QCursor", "QDesktopServices",
    "QDoubleValidator", "QDrag", "QDragEnterEvent", "QDragLeaveEvent",
    "QDragMoveEvent", "QDropEvent", "QEnterEvent", "QFocusEvent", "QFont",
    "QFontMetrics", "QFontMetricsF", "QGradient", "QGuiApplication",
    "QHideEvent", "QIcon", "QImage", "QImageReader", "QImageWriter",
    "QIntValidator", "QKeyEvent", "QKeySequence", "QLinearGradient",
    "QMouseEvent", "QPainter", "QPainterPath", "QPainterPathStroker",
    "QPaintEvent", "QPalette", "QPen", "QPixmap", "QPolygon", "QPolygonF",
    "QRadialGradient", "QRegion", "QResizeEvent", "QScreen", "QShortcut",
    "QShowEvent", "QSyntaxHighlighter", "QTextCharFormat", "QTextCursor",
    "QTextDocument", "QTransform", "QUndoCommand", "QValidator", "QWheelEvent",

    # Third-party
    "Image", "np",

    # Project types
    "Focus", "Event", "CommandSpec",

    # Utilities
    "clone_focus_pure", "draw_outlined_text", "format_project_file_size",
    "obfuscate_path", "obfuscate_text", "obfuscate_user_in_path",
    "pixmap_from_file_via_pillow", "safe_qt_call", "safe_ui_operation",
    "set_widget_path_display", "show_error", "convert_txt_to_project_dict",
]
