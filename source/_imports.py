"""Common imports for the project.

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
try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

# =============================================================================
# Environment-controlled optional imports
# =============================================================================
_skip_data_structs = os.environ.get("FOCUS_SKIP_DATASTRUCTS") == "1"
_skip_utils = os.environ.get("FOCUS_SKIP_UTILS") == "1"

# =============================================================================
# Project Data Structures
# =============================================================================
if TYPE_CHECKING:
    # For type checkers: always import the real types
    from _dataStructs import Focus, Event, CommandSpec
else:
    # For runtime: try to import, fall back to None if missing
    if not _skip_data_structs:
        try:
            from _dataStructs import Focus, Event, CommandSpec
        except ImportError:
            logger.warning("Failed to import _dataStructs; setting placeholders.")
            Focus = Event = CommandSpec = None  # type: ignore
    else:
        Focus = Event = CommandSpec = None  # type: ignore

# =============================================================================
# Utility Helpers
# =============================================================================
if TYPE_CHECKING:
    # For type checkers: always import the real functions
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
else:
    # For runtime: try to import, fall back to None if missing
    if not _skip_utils:
        try:
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
        except ImportError:
            logger.warning("Failed to import helpers from _utils; falling back to no-ops.")
            clone_focus_pure = None  # type: ignore
            draw_outlined_text = None  # type: ignore
            format_project_file_size = None  # type: ignore
            obfuscate_path = None  # type: ignore
            obfuscate_text = None  # type: ignore
            obfuscate_user_in_path = None  # type: ignore
            pixmap_from_file_via_pillow = None  # type: ignore
            safe_qt_call = None  # type: ignore
            safe_ui_operation = None  # type: ignore
            set_widget_path_display = None  # type: ignore
            show_error = None  # type: ignore
    else:
        clone_focus_pure = None  # type: ignore
        draw_outlined_text = None  # type: ignore
        format_project_file_size = None  # type: ignore
        obfuscate_path = None  # type: ignore
        obfuscate_text = None  # type: ignore
        obfuscate_user_in_path = None  # type: ignore
        pixmap_from_file_via_pillow = None  # type: ignore
        safe_qt_call = None  # type: ignore
        safe_ui_operation = None  # type: ignore
        set_widget_path_display = None  # type: ignore
        show_error = None  # type: ignore

# =============================================================================
# Optional Converters
# =============================================================================
if TYPE_CHECKING:
    # For type checkers: always import the real function
    from _txt_converter import convert_txt_to_project_dict
else:
    # For runtime: try to import, fall back to None if missing
    try:
        from _txt_converter import convert_txt_to_project_dict
    except ImportError:
        convert_txt_to_project_dict = None  # type: ignore

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
