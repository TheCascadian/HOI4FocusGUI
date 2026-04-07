"""
_nodes.py

Consolidated node class definitions for the Focus Tree Editor.
Contains all visual node representations and connection items.

Extracted from _focusGUI.py for better code organization and maintainability.

Classes:
- ConnectionItem: Base class for all visual connection items
- NodeBase: Shared helper mixin for node-like QGraphicsItems
- LShapedConnectionLine: L-shaped connection between focus nodes with 90-degree corners
- MutualExclusiveConnector: Visual connector showing mutual exclusion between focuses
- FocusNode: Visual representation of a focus with enhanced interaction
- EventNode: Visual representation of an event (supports off-grid placement with Ctrl)
- NoteNode: Resizable sticky note with title, color, and connections
- NoteConnectionLine: Curved connection line between two NoteNode objects
- NoteFocusConnector: Dashed connector between NoteNode and FocusNode
- NoteEventConnector: Dashed connector between NoteNode and EventNode
- EventFocusConnector: Connector between EventNode and FocusNode
- EventEventConnector: Connector between two EventNode objects
"""

import logging

from _imports import (
    # Standard library
    os, uuid,
    # Typing
    Any, Dict, List, Optional, TYPE_CHECKING,
    # PyQt Core
    QObject, QPointF, QRectF, Qt, QTimer,
    # PyQt GUI
    QAction, QBrush, QColor, QCursor, QFont, QImage,
    QPainter, QPainterPath, QPen, QPixmap, QTransform,
    # PyQt Widgets
    QApplication, QColorDialog, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGraphicsDropShadowEffect,
    QGraphicsItem, QGraphicsObject, QGraphicsPathItem,
    QGraphicsPixmapItem, QGraphicsSceneContextMenuEvent,
    QGraphicsSceneMouseEvent, QGraphicsTextItem, QGraphicsView,
    QInputDialog, QLineEdit, QMenu, QMessageBox,
    # Project types and utilities
    Event, Focus, draw_outlined_text, obfuscate_user_in_path,
    pixmap_from_file_via_pillow, safe_ui_operation,
)
from error_handler import ErrorPolicy, PolicyConfig, handle_exception, silent_operation


if TYPE_CHECKING:
    # Import commands and helpers only for type checking to avoid circular imports
    from _focusGUI import (
        RemoveConnectionCommand, SetIconCommand, MakeMutexCommand,
        NoteCreateLinkCommand, DeleteNoteCommand, MoveNoteCommand,
        ResizeNoteCommand, IconLibraryDialog
    )

# Optional PIL for image processing
try:
    from PIL import Image
except ImportError:
    Image = None

# Module logger
logger = logging.getLogger(__name__)

# Constants
GRID_UNIT = 300.0
FOCUS_WIDTH = 260.0
FOCUS_HEIGHT = 140.0

# ============================================================================
# Import command classes and helper functions from their proper modules
# ============================================================================

from _commands import (
    RemoveConnectionCommand, SetIconCommand, MakeMutexCommand,
    DeleteNoteCommand, MoveNoteCommand, ResizeNoteCommand
)

from _utils import (
    pixmap_from_file_via_pillow, draw_outlined_text, obfuscate_user_in_path
)

# Note: IconLibraryDialog will be imported later when needed (currently still in _focusGUI.py)

# ============================================================================
# region Base Connection & Node Classes
# ============================================================================

# ========== ConnectionItem (1042-1150) ==========
class ConnectionItem(QGraphicsPathItem):
    """Base class for visual connection items.

    Consolidates common pen/color setup and provides a safe removal helper that
    prefers the application's undo stack when available.
    """
    def __init__(self, color: Optional[QColor] = None, width: int = 2, z: float = -1.0):
        super().__init__()
        with silent_operation("init_color"):
            self.color = QColor(color) if color is not None else QColor(90, 90, 90)
        if not hasattr(self, 'color'):
            self.color = QColor(90, 90, 90)
        with silent_operation("init_pen"):
            pen = QPen(self.color, max(1, int(width)))
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.setPen(pen)
        with silent_operation("init_zvalue"):
            self.setZValue(z)
        # LOD / caching state to avoid rebuilding paths when nothing significant changed
        with silent_operation("init_lod_cache"):
            self._last_lod_mode = None
            self._last_endpoints = None
        if not hasattr(self, '_last_lod_mode'):
            self._last_lod_mode = None
            self._last_endpoints = None
    def _get_view_scale(self) -> float:
        """Return the current view scale (m11) for the first view showing this scene.

        Falls back to 1.0 when unavailable. This is used to decide LOD for connection
        rendering (simpler straight-line at low zoom).
        """
        with silent_operation("get_view_scale"):
            sc = self.scene()
            if sc is None:
                return 1.0
            views = sc.views()
            if not views:
                return 1.0
            # Use the first view's horizontal scale component (assumes uniform scaling)
            tr = views[0].transform()
            return float(tr.m11())
        return 1.0

    def _use_simple_lod(self, threshold: Optional[float] = None) -> bool:
        """Decide whether to use simple LOD rendering for this connection.

        The threshold may be overridden per-canvas via attribute
        `connection_lod_threshold` (float). If not present, default is 0.0 (fully disabled for fidelity).
        """
        with silent_operation("use_simple_lod"):
            sc = self.scene()
            canvas = None
            if sc is not None:
                canvas = getattr(sc, 'parent', None) or getattr(sc, 'editor', None) or getattr(sc, 'parentItem', None)
            if threshold is None:
                with silent_operation("get_lod_threshold"):
                    threshold = float(getattr(canvas, 'connection_lod_threshold', 0.0))
                if threshold is None:
                    threshold = 0.0
            return self._get_view_scale() < float(threshold)
        return False

    def _endpoints_state(self):
        """Return a lightweight, comparable representation of this connection's endpoints.

        This is used to decide whether the connection geometry needs rebuilding.
        Values are rounded to 2 decimal places to avoid tiny micro-movements causing rebuilds.
        """
        with silent_operation("endpoints_state"):
            def pos_of(obj):
                with silent_operation("pos_of"):
                    if obj is None:
                        return (0.0, 0.0)
                    if hasattr(obj, 'scenePos'):
                        p = obj.scenePos()
                        return (round(float(p.x()), 2), round(float(p.y()), 2))
                    if hasattr(obj, 'sceneBoundingRect'):
                        c = obj.sceneBoundingRect().center()
                        return (round(float(c.x()), 2), round(float(c.y()), 2))
                return (0.0, 0.0)

            pairs = [('start_node', 'end_node'), ('a', 'b'), ('note', 'focus_node'), ('note', 'event_node'), ('event_node', 'focus_node')]
            for p1, p2 in pairs:
                o1 = getattr(self, p1, None)
                o2 = getattr(self, p2, None)
                if o1 is not None and o2 is not None:
                    return pos_of(o1) + pos_of(o2)

            # Fallback to current path bounding rect center twice (safe default)
            with silent_operation("fallback_endpoints"):
                br = self.path().boundingRect()
                c = br.center()
                cx = round(float(c.x()), 2); cy = round(float(c.y()), 2)
                return (cx, cy, cx, cy)
        return (0.0, 0.0, 0.0, 0.0)

# ========== NodeBase (1151-1300) ==========
class NodeBase:
    """Shared helper for node-like QGraphicsItems.

    Intended to be used via composition: classes can call self.init_node(...) in
    their __init__ to configure common flags, hover support, and a hover timer.
    """
    def init_node(self, editor, movable=True, selectable=True, accept_drops=False):
        with silent_operation("init_node_flags"):
            if movable:
                self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
            if selectable:
                self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
            self.setAcceptHoverEvents(True)
            if accept_drops:
                self.setAcceptDrops(True)
            # hover state
            with silent_operation("init_hover_state"):
                self.hovered = False
            # hover timer (lineage quick view)
            with silent_operation("init_hover_timer"):
                self._hover_timer = QTimer()
                self._hover_timer.setSingleShot(True)
                self._hover_timer.setInterval(1000)
                # if the owner class implements _on_hover_timeout it will be called
                with silent_operation("connect_hover_timeout"):
                    self._hover_timer.timeout.connect(self._on_hover_timeout)
            # move tracking
            with silent_operation("init_move_tracking"):
                self._move_start_scene_pos = None

    def set_color(self, color) -> None:
        col = QColor(90, 90, 90)  # default
        with silent_operation("parse_color"):
            if isinstance(color, str):
                col = QColor(str(color))
            elif isinstance(color, QColor):
                col = color
            else:
                col = QColor(color)
        self.color = col
        with silent_operation("apply_pen_color"):
            p = QPen(col, max(1, self.pen().width()))
            p.setCosmetic(True)
            p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.setPen(p)

    def safe_remove(self) -> None:
        """Remove this item from its scene, preferring an undoable command when available."""
        with silent_operation("safe_remove"):
            sc = self.scene()
            if sc is None:
                return
            main = getattr(sc, 'parent', None) or getattr(sc, 'editor', None) or None
            uw = getattr(main, 'undo_stack', None) if main is not None else None
            if uw is not None:
                with silent_operation("push_undo_command"):
                    uw.push(RemoveConnectionCommand(sc, self))
                    return
            # fallback to scene removal helpers
            if hasattr(sc, 'safe_remove_item'):
                with silent_operation("safe_remove_item"):
                    sc.safe_remove_item(self)
                    return
            with silent_operation("remove_item"):
                sc.removeItem(self)

    def apply_hidden_visibility(self) -> None:
        """Enforce hidden-focus visibility rules for nodes and connections.

        Hidden focuses (focus.hidden == True) are kept invisible unless one of
        their hidden_tags is explicitly enabled in self._show_hidden_branches_by_tag.
        This method also hides any connections attached to hidden nodes.
        """
        with silent_operation("apply_hidden_visibility"):
            for nid, node in list(getattr(self, 'nodes', {}).items()):
                with silent_operation("process_node_visibility"):
                    fobj = getattr(node, 'focus', None)
                    if fobj is None:
                        continue
                    if getattr(fobj, 'hidden', False):
                        tags = list(getattr(fobj, 'hidden_tags', []) or [])
                        allowed = False
                        for t in tags:
                            with silent_operation("check_tag"):
                                if self._show_hidden_branches_by_tag.get(t, False):
                                    allowed = True
                                    break
                        # If not allowed, hide the node but keep incoming connections visible
                        # (they will be styled as 'hidden branch' in painting). Outgoing
                        # connections from a hidden node should be hidden.
                        if not allowed:
                            with silent_operation("hide_node"):
                                node.setVisible(False)
                            # Keep incoming visible so they can be drawn as a dashed 'hidden' connector.
                            for conn in list(getattr(node, 'connections_in', []) or []):
                                with silent_operation("show_incoming_conn"):
                                    conn.setVisible(True)
                                    with silent_operation("update_conn_path"):
                                        conn.update_path()
                            # Hide outgoing connections from the hidden node
                            for conn in list(getattr(node, 'connections_out', []) or []):
                                with silent_operation("hide_outgoing_conn"):
                                    conn.setVisible(False)
                        else:
                            # if allowed, ensure connections for this node are visible where appropriate
                            for conn in list(getattr(node, 'connections_in', []) or []) + list(getattr(node, 'connections_out', []) or []):
                                with silent_operation("show_allowed_conn"):
                                    # visibility for connection will be determined by cull/update logic; ensure it's visible now
                                    conn.setVisible(True)

# endregion

# region Focus Node Connections

class LShapedConnectionLine(ConnectionItem): # connection line
    """L-shaped connection between focuses with 90-degree corners"""
    def __init__(self, start_node, end_node):
        # determine configured width first so base class can use it
        w = 2  # default
        with silent_operation("get_connection_line_width"):
            w = int(getattr(getattr(start_node, 'editor', None).canvas, 'connection_line_width', 2))
        super().__init__(QColor(Qt.GlobalColor.blue), width=w, z=-1)
        self.start_node = start_node
        self.end_node = end_node
        self.update_path()

    def update_path(self):
        """Update the L-shaped path between nodes"""
        # Early-out: skip rebuild if endpoints and LOD haven't changed
        with silent_operation("update_path_lod_cache_check"):
            current_lod = self._use_simple_lod()
            current_eps = self._endpoints_state()
            if self._last_lod_mode == current_lod and self._last_endpoints == current_eps:
                return
            self._last_lod_mode = current_lod
            self._last_endpoints = current_eps
        # LOD: if the view is zoomed out beyond the threshold, draw a simple straight
        # line between anchors to reduce painting cost.
        with silent_operation("update_path_simple_lod"):
            if self._use_simple_lod():
                start_pos = self.start_node.scenePos()
                end_pos = self.end_node.scenePos()
                sp = QPointF(start_pos.x(), start_pos.y() + 30)
                ep = QPointF(end_pos.x(), end_pos.y() - 30)
                path = QPainterPath(sp)
                path.lineTo(ep)
                self.setPath(path)
                return
        start_pos = self.start_node.scenePos()
        end_pos = self.end_node.scenePos()
        # Calculate connection points (bottom of start node to top of end node)
        start_x = start_pos.x()
        start_y = start_pos.y() + 30  # Bottom of start node
        end_x = end_pos.x()
        end_y = end_pos.y() - 30  # Top of end node

        path = QPainterPath()
        path.moveTo(start_x, start_y)

        # Create L-shaped path
        if abs(end_x - start_x) > abs(end_y - start_y):
            # Horizontal then vertical
            mid_y = start_y + (end_y - start_y) * 0.5
            # Slight curve at elbow using quadTo for nicer appearance
            path.lineTo(start_x, mid_y - 1)
            path.quadTo(QPointF(start_x, mid_y), QPointF(start_x + (end_x - start_x) * 0.15, mid_y))
            path.lineTo(end_x - (end_x - start_x) * 0.15, mid_y)
            path.quadTo(QPointF(end_x, mid_y), QPointF(end_x, mid_y + (end_y - start_y) * 0.15))
            path.lineTo(end_x, end_y)
        else:
            # Vertical then horizontal
            mid_x = start_x + (end_x - start_x) * 0.5
            path.lineTo(mid_x - 1, start_y)
            path.quadTo(QPointF(mid_x, start_y), QPointF(mid_x, start_y + (end_y - start_y) * 0.15))
            path.lineTo(mid_x, end_y - (end_y - start_y) * 0.15)
            path.quadTo(QPointF(mid_x, end_y), QPointF(mid_x + (end_x - start_x) * 0.15, end_y))
            path.lineTo(end_x, end_y)

        # Add arrowhead at the end
        arrow_size = 8
        # Compute a simple triangle pointing leftwards (approx)
        arrow_head = QPainterPath()
        arrow_head.moveTo(end_x, end_y)
        arrow_head.lineTo(end_x - arrow_size * 0.866, end_y - arrow_size * 0.5)
        arrow_head.lineTo(end_x - arrow_size * 0.866, end_y + arrow_size * 0.5)
        arrow_head.closeSubpath()
        path.addPath(arrow_head)

        self.setPath(path)
        # Solid line only (no gradients)
        with silent_operation("set_pen_styling"):
            # ensure pen uses configured width
            w = 2
            with silent_operation("get_line_width"):
                w = int(getattr(getattr(self.start_node, 'editor', None).canvas, 'connection_line_width', self.pen().width()))
            # Default pen uses the connection base color. However, if this
            # connection represents a prerequisite group (AND/OR) we want to
            # preserve a dashed colored style across path updates. Check for
            # prereq_kind and override accordingly.
            # First, if the destination focus is hidden and the end node itself
            # is not visible, apply a special gray dashed style to indicate
            # a hidden branch (like the game does). This keeps the incoming
            # connection visible while the node remains hidden.
            is_hidden_branch = False
            with silent_operation("check_hidden_branch"):
                end_focus = getattr(self.end_node, 'focus', None)
                if end_focus and getattr(end_focus, 'hidden', False) and not self.end_node.isVisible():
                    is_hidden_branch = True

            if is_hidden_branch:
                p = QPen(QColor(140, 140, 140), max(1, w), Qt.PenStyle.DashLine)
                p.setCosmetic(True)
                p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setCapStyle(Qt.PenCapStyle.RoundCap)
                self.setPen(p)
            else:
                kind = getattr(self, 'prereq_kind', None)
                if kind is not None:
                    col = self.color
                    with silent_operation("get_prereq_color"):
                        if str(kind).upper() == 'AND':
                            col = QColor(255, 220, 0)
                        else:
                            col = QColor(255, 140, 0)
                    p = QPen(col, max(1, w))
                    p.setCosmetic(True)
                    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    p.setCapStyle(Qt.PenCapStyle.RoundCap)
                    p.setStyle(Qt.PenStyle.DashLine)
                    self.setPen(p)
                else:
                    p = QPen(self.color, max(1, w))
                    p.setCosmetic(True)
                    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    p.setCapStyle(Qt.PenCapStyle.RoundCap)
                    self.setPen(p)
    def set_color(self, color: QColor) -> None:
        """Set the base color for the connection and update pen gradient.

        Accepts QColor or any object convertible to QColor.
        """
        col = QColor(Qt.GlobalColor.blue)
        with silent_operation("parse_color"):
            if isinstance(color, (str,)):
                col = QColor(str(color))
            elif isinstance(color, QColor):
                col = color
            else:
                # attempt to coerce
                col = QColor(color)
        self.color = col
        # If this connection points to a hidden focus whose node is currently invisible,
        # force the special gray dashed 'hidden branch' style and do not override it.
        with silent_operation("check_hidden_branch"):
            end_focus = getattr(getattr(self, 'end_node', None), 'focus', None)
            if end_focus and getattr(end_focus, 'hidden', False) and not getattr(self.end_node, 'isVisible', lambda: True)():
                w = 2
                with silent_operation("get_line_width"):
                    w = int(getattr(getattr(self.start_node, 'editor', None).canvas, 'connection_line_width', 2))
                p = QPen(QColor(140, 140, 140), max(1, w), Qt.PenStyle.DashLine)
                p.setCosmetic(True)
                p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setCapStyle(Qt.PenCapStyle.RoundCap)
                self.setPen(p)
                return

        # update pen to a simple solid pen with cosmetic width and configured width
        w = 2
        with silent_operation("get_line_width_default"):
            w = int(getattr(getattr(self.start_node, 'editor', None).canvas, 'connection_line_width', 2))
        p = QPen(col, max(1, w))
        p.setCosmetic(True)
        p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(p)

    def set_prereq_style(self, kind: Optional[str]) -> None:
        """Apply visual style when this connection represents a prerequisite from a group.

        kind: 'AND'|'OR' or None. AND -> yellow dashed medium length; OR -> orange dashed medium length.
        """
        with silent_operation("set_prereq_style"):
            # If the end node is a hidden focus that is currently invisible, force the
            # gray dashed hidden-branch style and don't allow prereq styling to override it.
            with silent_operation("check_hidden_branch"):
                end_focus = getattr(getattr(self, 'end_node', None), 'focus', None)
                if end_focus and getattr(end_focus, 'hidden', False) and not getattr(self.end_node, 'isVisible', lambda: True)():
                    w = 2
                    with silent_operation("get_line_width"):
                        w = int(getattr(getattr(self.start_node, 'editor', None).canvas, 'connection_line_width', 2))
                    p = QPen(QColor(140, 140, 140), max(1, w), Qt.PenStyle.DashLine)
                    p.setCosmetic(True)
                    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    p.setCapStyle(Qt.PenCapStyle.RoundCap)
                    self.setPen(p)
                    return

            if kind is None:
                # revert to default color handling
                w = 2
                with silent_operation("get_line_width_default"):
                    w = int(getattr(getattr(self.start_node, 'editor', None).canvas, 'connection_line_width', 2))
                p = QPen(self.color, max(1, w))
                p.setCosmetic(True)
                p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setCapStyle(Qt.PenCapStyle.RoundCap)
                self.setPen(p)
                return
            # determine color and dash
            if str(kind).upper() == 'AND':
                col = QColor(255, 220, 0)  # yellow
            else:
                col = QColor(255, 140, 0)  # orange
            w = 2
            with silent_operation("get_line_width_prereq"):
                w = int(getattr(getattr(self.start_node, 'editor', None).canvas, 'connection_line_width', 2))
            p = QPen(col, max(1, w))
            p.setCosmetic(True)
            p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setCapStyle(Qt.PenCapStyle.RoundCap)
            # dashed pattern: medium length dashes
            p.setStyle(Qt.PenStyle.DashLine)
            self.setPen(p)

# endregion

# region Mutual Exclusion Connector

class MutualExclusiveConnector(QGraphicsPathItem):
    """Visual connector showing a mutual exclusion (---<!>---) between two FocusNodes.

    This implementation prefers to render a small icon at the midpoint using the
    game's art (DDS/PNG). It supports a Pillow-based SSAA path and multiple
    fallbacks so it works even when Pillow isn't installed.
    """
    def __init__(self, a: 'FocusNode', b: 'FocusNode'):
        super().__init__()
        self.a = a
        self.b = b
        # Keep the path under nodes; draw the icon as a separate child item above the path
        self.setZValue(-0.5)
        # Make mutex line red and dashed to stand out
        pen = QPen(QColor(200, 40, 40), 2, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.setPen(pen)
        # attempt to load a replacement pixmap for the diamond marker
        self._pixmap = None
        # if a PNG fallback was used/created, store its path so we can SSAA the PNG reliably
        self._mutex_icon_png_path = None
        self._mutex_icon_frame_w = None
        self._mutex_icon_frame_h = None
        with silent_operation("load_mutex_icon"):
            # Prefer app base dir if available
            abd = None
            with silent_operation("get_app_base_dir"):
                abd = getattr(getattr(self.a, 'editor', None), 'app_base_dir', None)
            candidates = []
            # If the editor exposes a helper to discover Steam libraries, use it to search all libraries
            libs = []
            with silent_operation("get_steam_libraries"):
                editor = getattr(self.a, 'editor', None)
                if editor is not None and hasattr(editor, '_steam_library_candidates'):
                    with silent_operation("call_steam_library_candidates"):
                        libs = editor._steam_library_candidates() or []
            # Prepend abd if provided (it may be a game install folder root)
            with silent_operation("prepend_app_base_dir"):
                if abd:
                    libs.insert(0, abd)
            # Build candidate file paths from discovered libraries
            for lib in libs:
                with silent_operation("build_candidate_path"):
                    cand = os.path.join(lib, 'common', 'Hearts of Iron IV', 'gfx', 'interface', 'focusview', 'focus_link_exclusive.dds')
                    candidates.append(cand)
            # Also try common SteamLibrary in the user's home as a sensible default
            with silent_operation("add_home_steam_path"):
                home_cand = os.path.join(os.path.expanduser('~'), 'SteamLibrary', 'steamapps', 'common', 'Hearts of Iron IV', 'gfx', 'interface', 'focusview', 'focus_link_exclusive.dds')
                candidates.append(home_cand)
            # fallback relative path
            candidates.append(os.path.join('gfx', 'interface', 'focusview', 'focus_link_exclusive.dds'))
            for c in candidates:
                with silent_operation("try_load_candidate"):
                    if not c or not os.path.isfile(c):
                        continue
                    # Primary: let QPixmap try to load it directly
                    pm = QPixmap(c)
                    # If that failed, try a sibling PNG first
                    if (not pm or pm.isNull()):
                        with silent_operation("try_png_sibling"):
                            base, ext = os.path.splitext(c)
                            png_path = base + '.png'
                            if os.path.isfile(png_path):
                                pm = QPixmap(png_path)
                    # If still not loaded and PIL is available, try converting DDS -> PNG on disk
                    if (not pm or pm.isNull()) and Image is not None and c.lower().endswith('.dds'):
                        with silent_operation("convert_dds_to_png"):
                            base, ext = os.path.splitext(c)
                            png_path = base + '.png'
                            if not os.path.isfile(png_path):
                                with silent_operation("save_png"):
                                    img = Image.open(c)
                                    img = img.convert('RGBA')
                                    img.save(png_path, format='PNG')
                            if os.path.isfile(png_path):
                                pm = QPixmap(png_path)
                    # In-memory PIL -> QImage fallback (no disk write)
                    if (not pm or pm.isNull()) and Image is not None and c.lower().endswith('.dds'):
                        with silent_operation("pil_to_qimage"):
                            img = Image.open(c)
                            img = img.convert('RGBA')
                            data = img.tobytes('raw', 'RGBA')
                            qim = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
                            pm = QPixmap.fromImage(qim)

                    if pm and not pm.isNull():
                        w = pm.width(); h = pm.height()
                        with silent_operation("crop_sprite_frame"):
                            # Heuristic: the sprite sheet usually contains equally-sized frames
                            # laid out horizontally. Prefer a square frame (height x height)
                            # if it divides evenly; otherwise try common splits (3 or 2 frames).
                            frame_w = None
                            if h > 0 and w >= h and (w % h) == 0:
                                frame_w = h
                            elif w % 3 == 0:
                                frame_w = w // 3
                            elif w % 2 == 0:
                                frame_w = w // 2
                            else:
                                # fallback: take roughly the left half as before
                                frame_w = max(1, int(w / 2))
                            crop = pm.copy(0, 0, max(1, int(frame_w)), h)
                            self._pixmap = crop
                            # store frame dims for potential PNG-based SSAA
                            with silent_operation("store_frame_dims"):
                                self._mutex_icon_frame_w = int(frame_w)
                                self._mutex_icon_frame_h = int(h)
                            # if we loaded from a PNG sibling, remember its path so paint() can prefer it
                            with silent_operation("store_png_path"):
                                if 'png_path' in locals() and os.path.isfile(png_path):
                                    self._mutex_icon_png_path = png_path
                            break
        # Create a child pixmap item to ensure the icon draws above the dashed line
        with silent_operation("create_pixmap_item"):
            from PyQt6.QtWidgets import QGraphicsPixmapItem
            self._pixmap_item = QGraphicsPixmapItem(parent=self)
            # place pixmap above the dashed line but below most nodes
            with silent_operation("set_pixmap_z_value"):
                self._pixmap_item.setZValue(0.0)
            self._pixmap_item.setVisible(False)
        # Position and set pixmap now
        self.update_path()

    def update_path(self):
        with silent_operation("update_mutex_path"):
            pa = self.a.scenePos(); pb = self.b.scenePos()
            # connect centers with a horizontal-ish path and draw <!> marker at midpoint
            a_pt = QPointF(pa.x(), pa.y())
            b_pt = QPointF(pb.x(), pb.y())
            # draw a simple straight path between centers; the visual marker
            # (diamond) will be replaced by an icon drawn in paint().
            path = QPainterPath(a_pt)
            path.lineTo(b_pt)
            self.setPath(path)
            # Update the pixmap child (build scaled QPixmap and position it at midpoint)
            with silent_operation("check_pixmap"):
                if self._pixmap is None or self._pixmap_item is None:
                    if self._pixmap_item is not None:
                        with silent_operation("hide_pixmap_item"):
                            self._pixmap_item.setVisible(False)
                    return
            with silent_operation("position_pixmap"):
                mid = (QPointF(pa.x(), pa.y()) + QPointF(pb.x(), pb.y())) * 0.5
                # Determine desired icon size in pixels
                canvas = None
                with silent_operation("get_canvas"):
                    canvas = getattr(self.a, 'editor', None).canvas if getattr(self.a, 'editor', None) is not None else None
                disp_scale = 1.0
                with silent_operation("get_disp_scale"):
                    disp_scale = float(getattr(canvas, 'mutex_icon_display_scale', 1.0) or 1.0)
                ICON_PX = int(round(18 * disp_scale))
                ss = 1.0
                with silent_operation("get_supersample_scale"):
                    ss = float(getattr(canvas, 'mutex_icon_supersample_scale', 1.0)) if canvas is not None else 1.0

                pm = None
                with silent_operation("scale_pixmap"):
                    if ss > 1.0 and getattr(self, '_mutex_icon_png_path', None) and Image is not None and self._mutex_icon_frame_w and self._mutex_icon_frame_h:
                        with silent_operation("pil_ssaa_scale"):
                            pil_img = Image.open(self._mutex_icon_png_path).convert('RGBA')
                            fw = int(self._mutex_icon_frame_w)
                            fh = int(self._mutex_icon_frame_h)
                            if pil_img.width >= fw and pil_img.height >= fh:
                                pil_img = pil_img.crop((0, 0, fw, fh))
                            oversample_boost = 1.25
                            up_w = max(1, int(ICON_PX * ss * oversample_boost))
                            up_h = max(1, int(ICON_PX * ss * oversample_boost))
                            resample_filter = getattr(Image, 'Resampling', None)
                            if resample_filter is not None:
                                resample = Image.Resampling.LANCZOS
                            else:
                                resample = Image.LANCZOS
                            pil_up = pil_img.resize((up_w, up_h), resample=resample)
                            with silent_operation("apply_unsharp_mask"):
                                from PIL import ImageFilter
                                pil_up = pil_up.filter(ImageFilter.UnsharpMask(radius=1, percent=80, threshold=2))
                            pil_final = pil_up.resize((ICON_PX, ICON_PX), resample=resample)
                            data = pil_final.tobytes('raw', 'RGBA')
                            qim = QImage(data, pil_final.width, pil_final.height, QImage.Format.Format_RGBA8888)
                            pm = QPixmap.fromImage(qim)
                            with silent_operation("set_dpr"):
                                pm.setDevicePixelRatio(1.0)
                    elif ss > 1.0:
                        src_img = None
                        with silent_operation("get_src_img"):
                            src_img = self._pixmap.toImage()
                        if src_img is None:
                            src_img = QPixmap(self._pixmap).toImage()
                        with silent_operation("convert_format"):
                            src_img = src_img.convertToFormat(QImage.Format.Format_RGBA8888)
                        oversample_boost = 1.25
                        up_w = max(1, int(ICON_PX * ss * oversample_boost))
                        up_h = max(1, int(ICON_PX * ss * oversample_boost))
                        tmp_img = src_img.scaled(up_w, up_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                        final_img = tmp_img.scaled(int(ICON_PX), int(ICON_PX), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                        pm = QPixmap.fromImage(final_img)
                        with silent_operation("set_dpr_qt"):
                            pm.setDevicePixelRatio(1.0)
                    else:
                        pm = self._pixmap.scaled(ICON_PX, ICON_PX, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                        with silent_operation("set_dpr_simple"):
                            pm.setDevicePixelRatio(1.0)

                if pm is not None and not pm.isNull():
                    with silent_operation("set_pixmap_pos"):
                        # set the pixmap on the child item and center it at midpoint
                        self._pixmap_item.setPixmap(pm)
                        w = pm.width(); h = pm.height()
                        with silent_operation("set_offset"):
                            self._pixmap_item.setOffset(-w/2, -h/2)
                        # position in parent's coordinates
                        with silent_operation("map_to_scene"):
                            local_mid = self.mapFromScene(mid)
                            self._pixmap_item.setPos(local_mid)
                        # ensure the pixmap draws above the connector by a small margin
                        with silent_operation("set_z_value"):
                            self._pixmap_item.setZValue(self.zValue() + 0.1)
                        self._pixmap_item.setVisible(True)
                else:
                    with silent_operation("hide_pixmap_null"):
                        self._pixmap_item.setVisible(False)

    def paint(self, painter: QPainter, option, widget):
        # draw the line/path first
        with silent_operation("paint_mutex_connector"):
            super().paint(painter, option, widget)
        # pixmap is handled by a child QGraphicsPixmapItem so nothing more to do here

    def set_color(self, color: QColor):
        """Set the color of the connection line and update pen."""
        with silent_operation("set_mutex_color"):
            self.color = color
            self.setPen(QPen(self.color, 2))

# endregion

# region Focus Node

class FocusNode(QGraphicsItem): # focus node
    """Visual representation of a focus with enhanced interaction"""
    # Class-level icon cache with LRU eviction for O(1) lookups
    _icon_cache: Dict[str, QPixmap] = {}  # QPixmap cache by path
    _icon_cache_size: float = 0.0  # Cumulative cache size in MB
    MAX_ICON_CACHE_MB: float = 50.0  # Maximum cache size before eviction

    def __init__(self, focus: Focus, editor):
        super().__init__()
        self.focus = focus
        self.editor = editor

        # Cache frequently accessed attributes to avoid getattr() on every paint call
        self._cached_canvas = None
        self._cached_scale = 1.0
        self._cached_tint = None
        self._cache_valid = False
        self._paint_safe = None  # Tri-state: None=unchecked, True=safe, False=fallback

        # common node setup (flags, hover timer, move tracking)
        with silent_operation("focus_node_init"):
            # initialize shared node state via NodeBase helper to ensure
            # hovered/_hover_timer/_move_start_scene_pos are present
            with silent_operation("init_node_base"):
                NodeBase.init_node(self, editor, movable=True, selectable=True, accept_drops=True)
        # Connection tracking (node-specific lists kept here)
        self.connections_in = []    # Connections coming into this node
        self.connections_out = []   # Connections going out from this node
        # Cross-type auxiliary connectors (non-prereq): initialized for updates on move
        self.note_focus_connectors = []
        self.event_focus_connectors = []
        self.mutex_connectors = []

        # Visibility control state
        # logical_visible: the intention/semantic visibility for this node (set by user toggles or auto rules)
        # user_override: when True, indicates the user explicitly set visibility (e.g., via GameStateDock)
        # Both are used to compute the actual scene visibility during cull/update passes.
        self.logical_visible = True
        self.user_override = False
        # Always set NoCache mode - we'll rely on manual update() calls for dynamic scaling
        # This ensures nodes always render fresh when zoom changes
        with silent_operation("set_cache_mode"):
            from PyQt6.QtWidgets import QGraphicsItem as _QI
            self.setCacheMode(_QI.CacheMode.NoCache)

    def invalidate_cache(self) -> None:
        """Call when canvas/editor state changes to invalidate cached values.

        Complexity: O(1)
        """
        self._cache_valid = False
        self._paint_safe = None

    @classmethod
    def load_pixmap_cached(cls, icon_path: str) -> Optional[QPixmap]:
        """Load pixmap with O(1) LRU cache to prevent repeated disk reads.

        Implements class-level cache with automatic eviction when size exceeds MAX_ICON_CACHE_MB.
        Evicts oldest 20% of entries when threshold is crossed.

        Args:
            icon_path: Path to icon file

        Returns:
            Cached QPixmap or None if load failed

        Complexity: O(1) average, O(n) worst case during eviction
        Space: O(n) where n is number of cached icons
        """
        if not icon_path:
            return None

        # Check memory cache first (O(1))
        if icon_path in cls._icon_cache:
            return cls._icon_cache[icon_path]

        # Load from disk
        pixmap = None
        with silent_operation("load_icon_pixmap_from_disk"):
            pixmap = QPixmap(icon_path)
            if pixmap.isNull():
                pixmap = None

        if pixmap is None:
            return None

        # Track cache size (4 bytes per RGBA pixel)
        size_mb = pixmap.width() * pixmap.height() * 4 / 1024 / 1024
        cls._icon_cache_size += size_mb

        # Evict oldest entries if over limit (LRU: remove first 20% of dict entries)
        if cls._icon_cache_size > cls.MAX_ICON_CACHE_MB:
            to_remove = max(1, len(cls._icon_cache) // 5)
            for key in list(cls._icon_cache.keys())[:to_remove]:
                old_px = cls._icon_cache.pop(key)
                with silent_operation("evict_cache_entry"):
                    old_size = old_px.width() * old_px.height() * 4 / 1024 / 1024
                    cls._icon_cache_size -= old_size

        cls._icon_cache[icon_path] = pixmap
        return pixmap

    def set_logical_visible(self, visible: bool, user: bool = False, force: bool = False) -> None:
        """Set the logical/semantic visibility for this node.

        - visible: desired logical visibility (True/False)
        - user: if True, mark this as a user-driven override (prevents non-forced automatic changes)
        - force: if True, apply the logical change even if a user override exists
        """
        with silent_operation("set_logical_visible"):
            if self.user_override and not force and not user:
                # user has explicitly forced state; ignore automated changes
                return
            self.logical_visible = bool(visible)
            if user:
                self.user_override = True
            # After logical change, update actual scene visibility according to current cull/hidden rules
            with silent_operation("apply_actual_visibility"):
                self._apply_actual_visibility()

    def clear_user_override(self) -> None:
        """Clear any user override so automatic rules may change logical visibility again."""
        with silent_operation("clear_user_override"):
            self.user_override = False

    def apply_scene_visible(self, visible: bool) -> None:
        """Set the item's scene visibility directly (bypass FocusNode.setVisible guard).

        Use this when the canvas cull/update system needs to show/hide the QGraphicsItem
        without triggering the hidden-tag enforcement in setVisible.
        """
        with silent_operation("apply_scene_visible"):
            super(FocusNode, self).setVisible(bool(visible))

    def _apply_actual_visibility(self) -> None:
        """Compute and apply the actual scene visibility from logical_visible and hidden-tag rules."""
        with silent_operation("apply_actual_visibility"):
            # Respect hidden-tag enforcement unless user_override is active
            allow = True
            with silent_operation("check_hidden_tags"):
                fobj = getattr(self, 'focus', None)
                if fobj is not None and getattr(fobj, 'hidden', False):
                    tags = list(getattr(fobj, 'hidden_tags', []) or [])
                    allowed = False
                    for t in tags:
                        with silent_operation("check_tag"):
                            if getattr(self.editor, 'canvas', None) and getattr(self.editor.canvas, '_show_hidden_branches_by_tag', {}).get(t, False):
                                allowed = True
                                break
                    if not allowed and not self.user_override:
                        allow = False
            final = bool(self.logical_visible and allow)
            with silent_operation("set_final_visibility"):
                # Use direct scene visibility setter to avoid our setVisible guard
                self.apply_scene_visible(final)

    def setVisible(self, visible: bool) -> None:
        """Override visibility to enforce hidden-focus rules.

        Any attempt to set a hidden focus visible will be blocked unless one
        of its hidden_tags is explicitly enabled on the canvas via
        canvas._show_hidden_branches_by_tag.
        Setting visible to False always works (used to hide nodes).
        """
        # Enforce hidden-focus rules deterministically. Use safe attribute
        # lookups so any missing attributes won't raise and accidentally
        # allow a hidden node to become visible. If any unexpected error
        # occurs while evaluating hidden semantics, default to keeping the
        # node hidden when the caller is trying to show it.
        fobj = getattr(self, 'focus', None)
        # Quick path: if caller isn't trying to show the node, allow hiding
        if not visible:
            with silent_operation("set_visible_false"):
                super().setVisible(False)
            return

        # Caller wants to show the node. If it's not marked hidden, allow.
        if fobj is None or not getattr(fobj, 'hidden', False):
            with silent_operation("set_visible_true"):
                super().setVisible(True)
            return

        # Safe canvas lookup (avoid chained getattr that can raise)
        canvas = None
        with silent_operation("get_canvas"):
            editor_obj = getattr(self, 'editor', None)
            canvas = getattr(editor_obj, 'canvas', None) if editor_obj is not None else None

        # Check hidden tags: only allow showing if one of the tags is enabled
        with silent_operation("check_hidden_tags"):
            tags = list(getattr(fobj, 'hidden_tags', []) or [])
            for t in tags:
                with silent_operation("check_single_tag"):
                    if canvas and getattr(canvas, '_show_hidden_branches_by_tag', {}).get(t, False):
                        with silent_operation("set_visible_allowed"):
                            super().setVisible(True)
                        return

        # No allowed tag — enforce hidden (don't make visible)
        with silent_operation("set_visible_blocked"):
            super().setVisible(False)

    @safe_ui_operation(default_return=1.0)
    def _get_dynamic_text_scale(self) -> float:
        """Compute dynamic text scale multiplier based on zoom level for better visibility on zoomed-out trees.

        Returns a scale factor >= 1.0 where:
        - 1.0 = normal size (at or above zoom threshold)
        - > 1.0 = scaled up (below zoom threshold) up to max_multiplier

        Scaling activates when zoom is below title_icon_scale_zoom_threshold and increases
        as zoom decreases, capped at title_icon_scale_max_multiplier for readability.

        Complexity: O(1) - decorator handles exceptions, eliminating nested try-except
        """
        canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
        if canvas is None:
            return 1.0

        # Check if dynamic scaling is enabled
        if not bool(getattr(canvas, 'enable_dynamic_title_icon_scaling', True)):
            return 1.0

        # Get current zoom scale from view
        views = getattr(canvas, 'views', lambda: [])()
        if not views:
            return 1.0

        current_zoom = views[0].transform().m11()

        threshold = float(getattr(canvas, 'title_icon_scale_zoom_threshold', 0.3))
        max_mult = float(getattr(canvas, 'title_icon_scale_max_multiplier', 2.5))

        # If at or above threshold, no scaling
        if current_zoom >= threshold:
            return 1.0

        # Below threshold: scale inversely with zoom
        # At threshold (0.3), scale = 1.0
        # At 0.15 (half threshold), scale = 2.0, etc
        # Formula: scale = threshold / current_zoom, capped at max_multiplier
        scale = threshold / max(current_zoom, 0.01)  # avoid division by zero
        return min(max(1.0, scale), max_mult)

    # Halo/selection glow extends 6px beyond the node rect; include margin in
    # boundingRect so Qt properly invalidates/repaints those pixels on deselect.
    _HALO_MARGIN = 10.0  # slightly larger than drawn halo to cover antialiasing

    def boundingRect(self):
        m = self._HALO_MARGIN
        return QRectF(-FOCUS_WIDTH/2.0 - m, -FOCUS_HEIGHT/2.0 - m, FOCUS_WIDTH + 2*m, FOCUS_HEIGHT + 2*m)

    def shape(self):
        """Return hit-test shape matching the actual node rect (not the expanded halo bounds).

        boundingRect includes halo margin for proper repaint/invalidation, but we don't
        want clicks in the halo area to select the node - only clicks on the node itself.
        """
        path = QPainterPath()
        path.addRoundedRect(QRectF(-FOCUS_WIDTH/2.0, -FOCUS_HEIGHT/2.0, FOCUS_WIDTH, FOCUS_HEIGHT), 5, 5)
        return path

    @safe_ui_operation(default_return=1.0)
    def _get_canvas_scale(self) -> float:
        """Get current view transform scale with O(1) decorator-based error handling.

        Returns:
            Current zoom scale from view transform, or 1.0 if unavailable

        Complexity: O(1) - decorator eliminates nested try-except blocks
        """
        canvas = self._cached_canvas
        if canvas is None or not hasattr(canvas, 'views'):
            return 1.0

        views = canvas.views()
        if not views:
            return 1.0

        return views[0].transform().m11()

    @safe_ui_operation(default_return=(0, 0))
    def _get_render_offsets(self, canvas) -> tuple:
        """Get focus render offsets with decorator-based error handling.

        Returns:
            Tuple of (offset_x, offset_y) for node positioning

        Complexity: O(1) - single decorator vs multiple try-except blocks
        """
        frx = int(getattr(canvas, 'focus_render_offset_x', 0) or 0)
        fry = int(getattr(canvas, 'focus_render_offset_y', 0) or 0)
        return (frx, fry)

    @safe_ui_operation(default_return=None)
    def _get_node_tint(self, canvas) -> Optional[QColor]:
        """Determine tint color for focus node with decorator-based error handling.

        Checks (in order):
        1. Canvas focus_color_overrides for this specific node
        2. Lineage color if node belongs to lineage
        3. Network color if node has network_id
        4. Canvas default_focus_color

        Returns:
            QColor tint or None if no color defined

        Complexity: O(1) - decorator eliminates nested try-except
        """
        if canvas is None:
            return None

        # Check override color first
        overrides = getattr(canvas, 'focus_color_overrides', None)
        if overrides:
            override = overrides.get(self.focus.id)
            if override is not None:
                return QColor(override)

        # Check lineage color
        lineage_map = getattr(canvas, '_lineage_of_node', None)
        if lineage_map:
            lid = lineage_map.get(self.focus.id)
            if lid:
                lineage_colors = getattr(canvas, '_lineage_colors', {})
                if lid in lineage_colors:
                    return QColor(lineage_colors[lid])

        # Check network color
        net = getattr(self.focus, 'network_id', None)
        if net is not None:
            network_colors = getattr(canvas, 'network_colors', {})
            if net in network_colors:
                return QColor(network_colors[net])

        # Default color
        default_color = getattr(canvas, 'default_focus_color', None)
        if default_color:
            return QColor(default_color)

        return None

    def paint(self, painter, option, widget):
        # Draw focus box with enhanced styling
        # Early exit for invisible nodes (O(1) check)
        with silent_operation("check_paint_visible"):
            if not self.isVisible():
                return

        # Base rectangle for the focus node centered at origin
        rect = QRectF(-FOCUS_WIDTH/2.0, -FOCUS_HEIGHT/2.0, FOCUS_WIDTH, FOCUS_HEIGHT)

        # Cache canvas reference to avoid repeated getattr() calls (reduces from 50+ to 1)
        if self._cached_canvas is None or not self._cache_valid:
            with silent_operation("cache_canvas"):
                self._cached_canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
                self._cache_valid = True
        canvas = self._cached_canvas

        def stack_value(element: str, attr: str, fallback: Any) -> Any:
            with silent_operation("stack_value"):
                getter = getattr(canvas, 'get_render_stack_value', None)
                if callable(getter):
                    val = getter(element, attr, fallback)
                    return fallback if val is None else val
            return fallback

        def _int_stack(element: str, attr: str, fallback: Any) -> int:
            val = stack_value(element, attr, fallback)
            with silent_operation("int_stack"):
                return int(val)
            return int(fallback or 0)

        def _float_stack(element: str, attr: str, fallback: Any) -> float:
            val = stack_value(element, attr, fallback)
            with silent_operation("float_stack"):
                return float(val)
            return float(fallback or 0.0)

        # If the current view transform is tiny (zoomed way out), skip icon and
        # pill drawing to reduce CPU/GPU cost; just draw a minimal placeholder.
        scale = self._get_canvas_scale()  # O(1) with decorator - replaces nested try-except

        _skip_rich_render = False
        with silent_operation("check_simple_render"):
            # Check simple rendering threshold setting
            simple_threshold = float(getattr(canvas, 'simple_render_zoom_threshold', 0.0))

            if simple_threshold > 0.0 and scale < simple_threshold:
                # Use simple rendering (colored rectangles) when zoomed below threshold
                # This provides performance boost on massive trees at extreme zoom-out
                _skip_rich_render = True

        # Apply global focus render offsets (move the whole node drawing)
        frx, fry = self._get_render_offsets(canvas)  # O(1) with decorator - replaces 2 try-except blocks
        if frx != 0 or fry != 0:
            rect = QRectF(rect.left() + frx, rect.top() + fry, rect.width(), rect.height())

        # Determine simulated availability via Game State dock (if present)
        is_unavailable = False
        with silent_operation("check_availability"):
            gs = getattr(self.editor, 'game_state_dock', None)
            conds = getattr(self.focus, 'avail_conditions', []) or []
            if gs is not None and conds:
                # simple evaluation: for now, if any condition requires a completed focus
                # (has_completed_focus) and that focus id is NOT marked completed in the dock,
                # consider the node unavailable. More complex boolean logic is intentionally
                # omitted here (would require full condition tree parsing).
                for c in conds:
                    with silent_operation("check_condition"):
                        if c.get('type') == 'has_completed_focus':
                            req = str(c.get('value'))
                            if not gs.is_completed(req):
                                is_unavailable = True
                                break

    # Determine tint/brush for the focus node (canvas already computed above)
        tint = self._get_node_tint(canvas)  # O(1) with decorator - replaces massive nested try-except

        if tint is None:
            base_brush = QColor(Qt.GlobalColor.lightGray)
        else:
            base_brush = QColor(Qt.GlobalColor.lightGray)
            with silent_operation("compute_base_brush"):
                h, s, l, a = tint.getHslF()
                base_brush = QColor()
                base_brush.setHslF(h, min(0.35, max(0.15, s*0.4)), min(0.85, max(0.6, l*1.1)), 1.0)

        # Selection/hover styling
        halo = None
        if self.isSelected():
            pen_color = QColor(255, 80, 80)
            brush_color = QColor(base_brush).lighter(115)
            halo = QColor(pen_color.red(), pen_color.green(), pen_color.blue(), 90)
        elif self.hovered:
            pen_color = QColor(40, 80, 200)
            brush_color = QColor(base_brush).lighter(130)
            halo = QColor(40, 80, 200, 60)
        else:
            pen_color = QColor(20, 20, 20)
            brush_color = base_brush

        # Draw halo (translucent rounded rect) if selection/hover
        with silent_operation("draw_halo"):
            if halo is not None:
                painter.save()
                try:
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(halo))
                    painter.drawRoundedRect(rect.adjusted(-6, -6, 6, 6), 10, 10)
                finally:
                    painter.restore()

        # If we're skipping rich render, draw minimal rect and return early
        if _skip_rich_render:
            with silent_operation("draw_simple_rect"):
                painter.save()
                if is_unavailable:
                    with silent_operation("set_unavailable_opacity"):
                        painter.setOpacity(0.5)
                painter.setPen(QPen(pen_color, 0))
                painter.setBrush(QBrush(brush_color))
                painter.drawRoundedRect(rect, 4, 4)
                painter.restore()
            return
        with silent_operation("draw_halo_again"):
            if halo is not None:
                painter.save()
                try:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(halo))
                    painter.drawRoundedRect(rect.adjusted(-6, -6, 6, 6), 10, 10)
                finally:
                    painter.restore()

        # Apply simulated unavailable visual treatment: lower overall opacity
        if is_unavailable:
            with silent_operation("set_opacity_unavailable"):
                painter.setOpacity(0.45)

        # Helper: load pixmap with class-level cache for O(1) lookups
        def load_pixmap(icon_path):
            """Load and cache QPixmap for the given path.

            Uses class-level LRU cache (load_pixmap_cached) for O(1) repeated lookups.
            Also respects canvas preference `prefer_pillow_tga` for .tga/.dds files.

            Complexity: O(1) on cache hit, O(disk I/O) on cache miss
            """
            if not icon_path:
                return None

            # Check per-focus instance cache first (legacy support)
            pix = getattr(self.focus, '_cached_icon_pixmap', None)
            if pix is not None and getattr(pix, 'path', None) == icon_path:
                return pix

            # Try class-level cache first (O(1))
            cached = FocusNode.load_pixmap_cached(icon_path)
            if cached is not None:
                return cached

            ext = os.path.splitext(str(icon_path))[1].lower()
            use_pillow_first = bool(getattr(canvas, 'prefer_pillow_tga', True)) and Image is not None and ext in ('.tga', '.dds')

            # Try Pillow first when requested and available
            if use_pillow_first:
                with silent_operation("load_pillow_first"):
                    alt = pixmap_from_file_via_pillow(icon_path)
                    if alt is not None:
                        with silent_operation("set_path_attr"):
                            setattr(alt, 'path', icon_path)
                        # Store in class cache for next time (O(1) future lookups)
                        FocusNode._icon_cache[icon_path] = alt
                        return alt

            # Qt fallback (preferred when Pillow not requested or fails)
            with silent_operation("load_qt_pixmap"):
                qp = QPixmap(icon_path)
                if qp and not qp.isNull():
                    with silent_operation("set_qt_path_attr"):
                        setattr(qp, 'path', icon_path)
                    # Store in class cache for next time
                    FocusNode._icon_cache[icon_path] = qp
                    return qp

            # If Qt failed and Pillow is available, try Pillow as a last resort
            if Image is not None and not use_pillow_first:
                with silent_operation("load_pillow_fallback"):
                    alt = pixmap_from_file_via_pillow(icon_path)
                    if alt is not None:
                        with silent_operation("set_fallback_path_attr"):
                            setattr(alt, 'path', icon_path)
                        FocusNode._icon_cache[icon_path] = alt
                        return alt

            # nothing worked
            setattr(self.focus, '_cached_icon_pixmap', None)
            return None

        # Helper: draw title as a pill using configured style (default/image/none)
        def draw_title_pill(painter: QPainter, pill_rect: QRectF, title_text: str, text_offset_x: float = 0.0, text_offset_y: float = 0.0):
            base_mode = getattr(canvas, 'title_pill_mode', 'default') if canvas is not None else 'default'
            mode = stack_value('pill', 'mode', base_mode)
            mode = (mode or 'default').lower()
            # Text prep
            fm = painter.fontMetrics()
            elided = fm.elidedText(title_text, Qt.TextElideMode.ElideRight, int(pill_rect.width() - 12))

            if mode == 'none':
                # No background; draw centered text only
                painter.save()
                try:
                    painter.setPen(QColor(250, 250, 250))
                    # apply text offset when no background is drawn
                    txt_rect = QRectF(pill_rect.x() + text_offset_x, pill_rect.y() + text_offset_y, pill_rect.width(), pill_rect.height())
                    painter.drawText(txt_rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine), elided)
                finally:
                    painter.restore()
                return

            if mode == 'image':
                # Try to draw a skinned pill image stretched to the rect
                try:
                    # Resolve cached pixmap on canvas if available and matching path
                    pill_pm = getattr(canvas, '_title_pill_pixmap', None)
                    img_path = getattr(canvas, 'title_pill_image_path', None) or ''
                    # If cached pixmap exists but path mismatches, drop cache
                    if pill_pm is not None and getattr(pill_pm, 'path', None) != img_path:
                        with silent_operation("delete_title_pill_pixmap_cache"):
                            delattr(canvas, '_title_pill_pixmap')
                        with silent_operation("delete_title_pill_pixmap_cache_fallback"):
                            del canvas._title_pill_pixmap
                        pill_pm = None

                    # Lazy-load from path if needed
                    if pill_pm is None and img_path:
                        loaded = None
                        try:
                            ext = os.path.splitext(str(img_path))[1].lower()
                            use_pillow_first = bool(getattr(canvas, 'prefer_pillow_tga', True)) and Image is not None and ext in ('.tga', '.dds')
                            # Try Pillow first for special formats when configured
                            if use_pillow_first:
                                loaded = pixmap_from_file_via_pillow(img_path)
                            # Qt fallback
                            if loaded is None:
                                qp = QPixmap(img_path) if os.path.exists(img_path) else None
                                if qp and not qp.isNull():
                                    loaded = qp
                            # If Qt failed and Pillow available, try Pillow as last resort
                            if loaded is None and Image is not None and not use_pillow_first:
                                loaded = pixmap_from_file_via_pillow(img_path)
                        except Exception:
                            with silent_operation("image_loading_fallback"):
                                loaded = None

                        if loaded is not None:
                            with silent_operation("set_loaded_path_attr"):
                                setattr(loaded, 'path', img_path)
                            with silent_operation("set_canvas_title_pill_pixmap"):
                                setattr(canvas, '_title_pill_pixmap', loaded)
                            with silent_operation("set_canvas_title_pill_pixmap_fallback"):
                                canvas._title_pill_pixmap = loaded
                            pill_pm = loaded

                    if pill_pm is not None and not getattr(pill_pm, 'isNull', lambda: False)():
                        # Determine native logical size of the pixmap (account for devicePixelRatio)
                        dpr = 1.0
                        with silent_operation("get_pixmap_device_pixel_ratio"):
                            dpr = float(pill_pm.devicePixelRatio())
                        if dpr == 1.0:
                            with silent_operation("get_pixmap_device_pixel_ratio_fallback"):
                                dpr = float(getattr(pill_pm, 'devicePixelRatio', 1.0)() or 1.0)
                        native_w = 0.0
                        native_h = 0.0
                        with silent_operation("calculate_native_pixmap_dimensions"):
                            native_w = float(pill_pm.width()) / (dpr if dpr else 1.0)
                            native_h = float(pill_pm.height()) / (dpr if dpr else 1.0)

                        # Expand pill_rect if necessary to accommodate native image size (add small padding)
                        pad = 8.0
                        with silent_operation("get_title_pill_padding"):
                            pad = _float_stack('pill', 'padding', getattr(canvas, 'title_pill_padding', 8))
                        desired_w = max(pill_rect.width(), native_w + pad)
                        desired_h = max(pill_rect.height(), native_h + pad)
                        if desired_w != pill_rect.width() or desired_h != pill_rect.height():
                            cx = pill_rect.center().x()
                            cy = pill_rect.center().y()
                            pill_rect = QRectF(cx - desired_w/2.0, cy - desired_h/2.0, desired_w, desired_h)

                        painter.save()
                        try:
                            if native_w <= 0 or native_h <= 0:
                                # Fallback: scale to pill rect if we can't determine native size
                                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                                sp = pill_pm.scaled(int(pill_rect.width()), int(pill_rect.height()), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                                painter.drawPixmap(pill_rect.topLeft(), sp)
                            else:
                                # Clip to pill rect so image doesn't draw outside its bounds
                                painter.setClipRect(pill_rect)
                                tx = pill_rect.center().x() - (native_w / 2.0)
                                ty = pill_rect.center().y() - (native_h / 2.0)
                                # Draw the full pixmap into the target rect sized to native logical pixels
                                target = QRectF(tx, ty, native_w, native_h)
                                source = QRectF(0, 0, float(pill_pm.width()), float(pill_pm.height()))
                                painter.drawPixmap(target, pill_pm, source)

                            # Draw text on top for readability, with optional outline
                            outline_enabled = True
                            outline_th = 1
                            outline_col = QColor('#000000')
                            with silent_operation("get_title_outline_settings"):
                                outline_enabled = bool(getattr(canvas, 'title_outline_enabled', True))
                                outline_th = int(getattr(canvas, 'title_outline_thickness', 1))
                                outline_col = QColor(getattr(canvas, 'title_outline_color', '#000000'))
                            # Draw outlined text using QPainterPath stroking (falls back to offset-draw on error)
                            txt_rect = QRectF(pill_rect.x() + text_offset_x, pill_rect.y() + text_offset_y, pill_rect.width(), pill_rect.height())
                            outlined_text_success = False
                            with silent_operation("draw_outlined_text_on_pill"):
                                draw_outlined_text(painter, txt_rect, [elided], painter.font(), outline_th, outline_col, QColor(245, 245, 245))
                                outlined_text_success = True
                            if not outlined_text_success:
                                # best-effort fallback
                                painter.setPen(QColor(245, 245, 245))
                                painter.drawText(QRectF(pill_rect.x() + text_offset_x, pill_rect.y() + text_offset_y, pill_rect.width(), pill_rect.height()), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine), elided)
                        finally:
                            painter.restore()
                        return
                    else:
                        # failed to load image; log and fall through to default pill
                        with silent_operation("log_title_pill_load_failure"):
                            logger.debug('Title pill image not found or failed to load: %s', img_path)
                except Exception:
                    with silent_operation("title_pill_fallback_to_default"):
                        pass  # Fallback to default style

            # Default vector pill background
            try:
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(36, 36, 36, 230)))
                # Respect title_pill_padding for vector pill sizing (pad around text)
                pad = 8.0
                with silent_operation("get_vector_pill_padding"):
                    pad = _float_stack('pill', 'padding', getattr(canvas, 'title_pill_padding', 8.0))
                text_w = pill_rect.width()
                text_h = pill_rect.height()
                with silent_operation("get_font_metrics_for_pill"):
                    fm_inside = painter.fontMetrics()
                    text_w = fm_inside.horizontalAdvance(elided)
                    text_h = fm_inside.height()
                desired_w = max(pill_rect.width(), text_w + pad * 2.0)
                desired_h = max(pill_rect.height(), text_h + pad)
                if desired_w != pill_rect.width() or desired_h != pill_rect.height():
                    cx = pill_rect.center().x(); cy = pill_rect.center().y()
                    pill_rect = QRectF(cx - desired_w/2.0, cy - desired_h/2.0, desired_w, desired_h)
                painter.drawRoundedRect(pill_rect, pill_rect.height()/2.0, pill_rect.height()/2.0)
                # subtle highlight
                painter.setBrush(QBrush(QColor(255, 255, 255, 20)))
                painter.drawRoundedRect(QRectF(pill_rect.left()+1, pill_rect.top()+1, pill_rect.width()-2, pill_rect.height()/2.0), (pill_rect.height()/2.0)-1, (pill_rect.height()/2.0)-1)
                # border
                pen = QPen(QColor(0, 0, 0, 200), 1)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(pill_rect, pill_rect.height()/2.0, pill_rect.height()/2.0)
                # text with optional outline
                outline_enabled = True
                outline_th = 1
                outline_col = QColor('#000000')
                with silent_operation("get_vector_pill_outline_settings"):
                    outline_enabled = bool(getattr(canvas, 'title_outline_enabled', True))
                    outline_th = int(getattr(canvas, 'title_outline_thickness', 1))
                    outline_col = QColor(getattr(canvas, 'title_outline_color', '#000000'))
                if outline_enabled and outline_th > 0:
                    txt_rect = QRectF(pill_rect.x() + text_offset_x, pill_rect.y() + text_offset_y, pill_rect.width(), pill_rect.height())
                    outlined_text_success = False
                    with silent_operation("draw_vector_pill_outlined_text"):
                        draw_outlined_text(painter, txt_rect, [elided], painter.font(), outline_th, outline_col, QColor(250, 250, 250))
                        outlined_text_success = True
                    if not outlined_text_success:
                        painter.setPen(QColor(250, 250, 250))
                        painter.drawText(QRectF(pill_rect.x() + text_offset_x, pill_rect.y() + text_offset_y, pill_rect.width(), pill_rect.height()), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine), elided)
                else:
                    painter.setPen(QColor(250, 250, 250))
                    painter.drawText(QRectF(pill_rect.x() + text_offset_x, pill_rect.y() + text_offset_y, pill_rect.width(), pill_rect.height()), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine), elided)
            finally:
                painter.restore()

        # Render icon-centric view
        if canvas is not None and getattr(canvas, 'icon_view_mode', False):
            # In icon view mode we intentionally do NOT draw the rounded rectangle
            # container — the focus should be represented only by its icon and
            # title (no rectangular background or border).

            # Helper: resolve icon path from focus.icon and library
            def resolve_icon_path(icon_val):
                if not icon_val:
                    return None
                result = icon_val
                with silent_operation("resolve_icon_library_path"):
                    lib = getattr(self.editor, 'icon_library', {})
                    if isinstance(lib, dict) and icon_val in lib:
                        result = lib.get(icon_val)
                return result

            # Helper: load pixmap with class-level cache for O(1) lookups
            def load_pixmap(icon_path):
                """Load and cache QPixmap for the given path using class-level LRU cache.

                Respects a Pillow helper when available for TGA/DDS files.
                Complexity: O(1) on cache hit, O(disk I/O) on cache miss
                """
                if not icon_path:
                    return None
                pix = getattr(self.focus, '_cached_icon_pixmap', None)
                if pix is not None and getattr(pix, 'path', None) == icon_path:
                    return pix

                # Try class-level cache first (O(1))
                cached = FocusNode.load_pixmap_cached(icon_path)
                if cached is not None:
                    return cached

                ext = os.path.splitext(str(icon_path))[1].lower()
                use_pillow_first = bool(getattr(canvas, 'prefer_pillow_tga', True)) and 'Image' in globals() and ext in ('.tga', '.dds')

                if use_pillow_first and 'pixmap_from_file_via_pillow' in globals():
                    with silent_operation("load_pixmap_via_pillow"):
                        alt = pixmap_from_file_via_pillow(icon_path)
                        if alt is not None:
                            with silent_operation("set_pixmap_path_attr"):
                                setattr(alt, 'path', icon_path)
                            FocusNode._icon_cache[icon_path] = alt
                            return alt

                with silent_operation("load_pixmap_via_qt"):
                    qp = QPixmap(icon_path)
                    if qp and not qp.isNull():
                        with silent_operation("set_qpixmap_path_attr"):
                            setattr(qp, 'path', icon_path)
                        FocusNode._icon_cache[icon_path] = qp
                        return qp

                return None

            icon_val = getattr(self.focus, 'icon', None)
            icon_path = resolve_icon_path(icon_val)
            pix = load_pixmap(icon_path) if icon_path else None

            # draw icon centered in the top area
            icon_bottom = rect.top() + rect.height() * 0.25
            try:
                icon_area_h = rect.height() * 0.62
                icon_area_w = rect.width() * 0.86
                max_dim = int(min(icon_area_h, icon_area_w))
                # Apply dynamic scaling for icon visibility at low zoom levels
                icon_scale = self._get_dynamic_text_scale()
                max_dim = int(max_dim * icon_scale)
                if pix and not pix.isNull():
                    # Super-sampled icon rendering
                    ssaa = float(getattr(canvas, 'icon_supersample_scale', 1.0) or 1.0)
                    tw = int(max_dim * ssaa)
                    th = int(max_dim * ssaa)
                    sp = pix.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    if ssaa > 1.0:
                        with silent_operation("set_scaled_pixmap_device_pixel_ratio"):
                            sp.setDevicePixelRatio(ssaa)
                    # compute logical device pixel ratio-aware sizes/position
                    dpr = 1.0
                    with silent_operation("get_scaled_pixmap_device_pixel_ratio"):
                        dpr = float(getattr(sp, 'devicePixelRatio', lambda: 1.0)() or 1.0)
                    logical_w = float(sp.width()) / (dpr if dpr else 1.0)
                    logical_h = float(sp.height()) / (dpr if dpr else 1.0)
                    x = rect.center().x() - logical_w/2.0
                    y = rect.top() + rect.height()*0.06

                    # optional subtle background behind icon when enabled
                    try:
                        if getattr(canvas, 'icon_view_show_background', False):
                            # Draw a slightly enlarged, low-opacity copy of the icon
                            # so the background matches the icon's silhouette instead of
                            # a simple rounded rectangle.
                            pad = 6
                            bg_scale = 1.12
                            try:
                                # Compute desired background logical size from logical icon size
                                logical_bg_w = logical_w * bg_scale
                                logical_bg_h = logical_h * bg_scale
                                # Scale the original pixmap to the background logical size.
                                # Do NOT use the supersampled `sp` dimensions here so SSAA
                                # doesn't affect the background rendering.
                                bg_tw = int(max(1, round(logical_bg_w)))
                                bg_th = int(max(1, round(logical_bg_h)))
                                sp_bg = pix.scaled(bg_tw, bg_th, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                                # compute logical sizes (pixmap may not use devicePixelRatio)
                                dpr_bg = 1.0
                                with silent_operation("get_background_pixmap_device_pixel_ratio"):
                                    dpr_bg = float(getattr(sp_bg, 'devicePixelRatio', lambda: 1.0)() or 1.0)
                                logical_bg_w = float(sp_bg.width()) / (dpr_bg if dpr_bg else 1.0)
                                logical_bg_h = float(sp_bg.height()) / (dpr_bg if dpr_bg else 1.0)
                                # position background centered at same horizontal center as icon
                                bg_x = rect.center().x() - logical_bg_w / 2.0
                                # vertically align the background to sit slightly behind the icon
                                bg_y = rect.top() + rect.height() * 0.06 - (logical_bg_h - logical_h) / 2.0
                                painter.save()
                                try:
                                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                                    painter.setOpacity(0.22)
                                    painter.drawPixmap(QPointF(bg_x, bg_y), sp_bg)
                                    painter.setOpacity(1.0)
                                finally:
                                    painter.restore()
                            except Exception:
                                with silent_operation("icon_background_draw_fallback"):
                                    # fallback: draw the rounded rect as before
                                    bx = x - pad
                                    by = y - pad
                                    bw = logical_w + pad * 2
                                    bh = logical_h + pad * 2
                                    painter.save()
                                    try:
                                        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                                        painter.setPen(Qt.PenStyle.NoPen)
                                        painter.setBrush(QBrush(QColor(32, 32, 32, 190)))
                                        painter.drawRoundedRect(QRectF(bx, by, bw, bh), min(bw, bh) * 0.12, min(bw, bh) * 0.12)
                                    finally:
                                        painter.restore()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    with silent_operation("icon_view_mode_draw_icon"):
                        pass

                    # Apply focus icon offsets from canvas / overrides
                    fx_off = 0
                    with silent_operation("get_focus_icon_offset_x"):
                        fx_off = _int_stack('icon', 'offset_x', getattr(canvas, 'focus_icon_offset_x', 0))
                    fy_off = 0
                    with silent_operation("get_focus_icon_offset_y"):
                        fy_off = _int_stack('icon', 'offset_y', getattr(canvas, 'focus_icon_offset_y', 0))
                    painter.drawPixmap(QPointF(x + fx_off, y + fy_off), sp)
                    icon_bottom = y + logical_h
                else:
                    # Display a safe, obfuscated path for icons to avoid exposing the user's home folder
                    if icon_path:
                        display_name = os.path.basename(str(icon_path))
                        with silent_operation("obfuscate_icon_path"):
                            display_name = obfuscate_user_in_path(str(icon_path))
                    else:
                        display_name = (str(icon_val) if icon_val else 'icon')
                    ph_w = int(min(120, max_dim))
                    ph_h = int(min(80, max_dim))
                    x = rect.center().x() - ph_w/2.0
                    y = rect.top() + rect.height()*0.08
                    painter.setPen(QPen(QColor(160,160,160), 1))
                    painter.setBrush(QBrush(QColor(240,240,240)))
                    painter.drawRect(QRectF(x, y, ph_w, ph_h))
                    painter.setFont(QFont('Arial', 9))
                    painter.setPen(QColor(80,80,80))
                    painter.drawText(QRectF(x+4, y+4, ph_w-8, ph_h-8), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), display_name)
                    icon_bottom = y + ph_h
            except Exception:
                with silent_operation("icon_view_draw_icon_fallback"):
                    pass

            # title below icon rendered as a rounded 'pill' label (dark pill, white bold text)
            base_pt = 12
            with silent_operation("get_icon_view_title_font_size"):
                base_pt = getattr(canvas, 'focus_title_font_size', 12) if canvas is not None else 12
                base_pt = int(base_pt)
            # Apply dynamic scaling for visibility at low zoom levels
            text_scale = self._get_dynamic_text_scale()
            scaled_pt = int(base_pt * text_scale)
            title_font = QFont('Arial', scaled_pt, QFont.Weight.Bold)
            painter.setFont(title_font)
            title = getattr(self.focus, 'title', None) or getattr(self.focus, 'name', None) or getattr(self.focus, 'focus_title', None) or self.focus.id
            title_margin_v = 6
            title_h = max(20, title_font.pointSize() + 10)
            title_w = rect.width() - 24
            # Determine pill background offsets (applies to the pill container)
            pill_tx = 0
            with silent_operation("get_icon_view_pill_offset_x"):
                pill_tx = _int_stack('pill', 'offset_x', getattr(canvas, 'focus_pill_offset_x', 0))
            pill_ty = 0
            with silent_operation("get_icon_view_pill_offset_y"):
                pill_ty = _int_stack('pill', 'offset_y', getattr(canvas, 'focus_pill_offset_y', 0))
            title_x = rect.center().x() - title_w / 2.0 + pill_tx
            title_y = icon_bottom + title_margin_v + pill_ty
            pill_rect = QRectF(title_x, title_y, title_w, title_h)
            # Determine text offsets (move title text only, inside the pill)
            text_tx = 0
            with silent_operation("get_icon_view_title_offset_x"):
                text_tx = _int_stack('title', 'offset_x', getattr(canvas, 'focus_title_offset_x', 0))
            text_ty = 0
            with silent_operation("get_icon_view_title_offset_y"):
                text_ty = _int_stack('title', 'offset_y', getattr(canvas, 'focus_title_offset_y', 0))
            draw_title_pill(painter, pill_rect, title, text_offset_x=text_tx, text_offset_y=text_ty)

            # small id line
            id_font = QFont('Arial', 8, QFont.Weight.Normal)
            painter.setFont(id_font)
            painter.setPen(QColor(60,60,60))
            if getattr(canvas, 'render_node_ids', True):
                id_text = f'[{self.focus.id}]'
                id_rect = QRectF(rect.left()+6, rect.bottom()-22, rect.width()-12, 16)
                painter.drawText(id_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), id_text)

            # prereq marker
            if self.focus.prerequisites:
                painter.setBrush(QBrush(Qt.GlobalColor.darkGreen))
                painter.drawEllipse(QRectF(rect.right()-22.0, rect.top()+10.0, 12.0, 12.0))
            # Draw hover coordinates near the mouse if available
            try:
                if getattr(self, 'hovered', False) and hasattr(self, '_hover_scene_pos') and getattr(self, '_hover_scene_pos') is not None:
                    # Convert scene pos to item local coordinates
                    sp = self._hover_scene_pos
                    local_pt = self.mapFromScene(sp)
                    # Grid coords (rounded)
                    gx = round(sp.x() / GRID_UNIT)
                    gy = round(sp.y() / GRID_UNIT)
                    coord_text = f"{gx}, {gy}"
                    fm = painter.fontMetrics()
                    text_w = fm.horizontalAdvance(coord_text) + 10
                    text_h = fm.height() + 6
                    # Position box to the right-bottom of cursor within item coords
                    bx = local_pt.x() + 12
                    by = local_pt.y() + 12
                    # Avoid drawing outside item rect by clamping inside node bounds
                    with silent_operation("clamp_coord_box_position"):
                        if bx + text_w > rect.right():
                            bx = local_pt.x() - text_w - 12
                    painter.save()
                    try:
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                        painter.setPen(QPen(QColor(30,30,30), 1))
                        painter.setBrush(QBrush(QColor(20,20,20,220)))
                        painter.drawRoundedRect(QRectF(bx, by, text_w, text_h), 4, 4)
                        painter.setPen(QColor(240,240,240))
                        painter.drawText(QRectF(bx+5, by+3, text_w-8, text_h-4), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), coord_text)
                    finally:
                        painter.restore()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return

        # Default rectangle node rendering (compact)
        pen_w = 4 if self.isSelected() else 2 if self.hovered else 2
        painter.setPen(QPen(pen_color, pen_w))
        painter.setBrush(QBrush(brush_color))
        painter.drawRoundedRect(rect, 5, 5)

        # title rendered as a rounded pill across the top of the node for consistency
        base_pt = 12
        with silent_operation("get_title_font_size"):
            base_pt = int(getattr(canvas, 'focus_title_font_size', 12) if canvas is not None else 12)
        # Apply dynamic scaling for visibility at low zoom levels
        text_scale = self._get_dynamic_text_scale()
        scaled_pt = int(base_pt * text_scale)
        title_font = QFont('Arial', scaled_pt, QFont.Weight.Bold)
        painter.setFont(title_font)
        title = getattr(self.focus, 'title', None) or getattr(self.focus, 'name', None) or getattr(self.focus, 'focus_title', None) or self.focus.id
        # pill sits near the top inside the rounded rect with some inset
        pill_h = max(20, scaled_pt + 8)
        pill_w = rect.width() - 24
        pill_tx = 0
        with silent_operation("get_regular_pill_offset_x"):
            pill_tx = _int_stack('pill', 'offset_x', getattr(canvas, 'focus_pill_offset_x', 0))
        pill_ty = 0
        with silent_operation("get_regular_pill_offset_y"):
            pill_ty = _int_stack('pill', 'offset_y', getattr(canvas, 'focus_pill_offset_y', 0))
            pill_ty = 0
        pill_x = rect.left() + (rect.width() - pill_w) / 2.0 + pill_tx
        pill_y = rect.top() + 8 + pill_ty
        pill_rect = QRectF(pill_x, pill_y, pill_w, pill_h)
        text_tx = 0
        with silent_operation("get_regular_title_offset_x"):
            text_tx = _int_stack('title', 'offset_x', getattr(canvas, 'focus_title_offset_x', 0))
        text_ty = 0
        with silent_operation("get_regular_title_offset_y"):
            text_ty = _int_stack('title', 'offset_y', getattr(canvas, 'focus_title_offset_y', 0))
        draw_title_pill(painter, pill_rect, title, text_offset_x=text_tx, text_offset_y=text_ty)

        # id line
        id_font = QFont('Arial', 8, QFont.Weight.Normal)
        painter.setFont(id_font)
        painter.setPen(QColor(60,60,60))
        if getattr(canvas, 'render_node_ids', True):
            id_text = f'[{self.focus.id}]'
            id_rect = QRectF(rect.left()+6, rect.bottom()-22, rect.width()-12, 16)
            painter.drawText(id_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), id_text)

        # prereq marker
        if self.focus.prerequisites:
            painter.setBrush(QBrush(Qt.GlobalColor.darkGreen))
            painter.drawEllipse(QRectF(rect.right()-22.0, rect.top()+10.0, 12.0, 12.0))

        # Draw hover coordinates near the mouse if available (default rendering path)
        with silent_operation("draw_default_hover_coords"):
            if getattr(self, 'hovered', False) and hasattr(self, '_hover_scene_pos') and getattr(self, '_hover_scene_pos') is not None:
                sp = self._hover_scene_pos
                local_pt = self.mapFromScene(sp)
                gx = round(sp.x() / GRID_UNIT)
                gy = round(sp.y() / GRID_UNIT)
                coord_text = f"{gx}, {gy}"
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(coord_text) + 10
                text_h = fm.height() + 6
                bx = local_pt.x() + 12
                by = local_pt.y() + 12
                with silent_operation("clamp_default_coord_box"):
                    if bx + text_w > rect.right():
                        bx = local_pt.x() - text_w - 12
                painter.save()
                try:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setPen(QPen(QColor(30,30,30), 1))
                    painter.setBrush(QBrush(QColor(20,20,20,220)))
                    painter.drawRoundedRect(QRectF(bx, by, text_w, text_h), 4, 4)
                    painter.setPen(QColor(240,240,240))
                    painter.drawText(QRectF(bx+5, by+3, text_w-8, text_h-4), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), coord_text)
                finally:
                    painter.restore()

        # small top-left icon
        icon_val = getattr(self.focus, 'icon', None)
        icon_path = resolve_icon_path(icon_val)
        pix = load_pixmap(icon_path) if icon_path else None
        if pix and not pix.isNull():
            with silent_operation("draw_small_topleft_icon"):
                icon_w = min(48, int(rect.width() * 0.18))
                icon_h = min(48, int(rect.height() * 0.18))
                ssaa = float(getattr(canvas, 'icon_supersample_scale', 1.0) or 1.0)
                tw = int(icon_w * ssaa)
                th = int(icon_h * ssaa)
                sp = pix.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                if ssaa > 1.0:
                    with silent_operation("set_icon_device_pixel_ratio"):
                        sp.setDevicePixelRatio(ssaa)
                painter.drawPixmap(QPointF(rect.left()+8, rect.top()+8), sp)
        else:
            if icon_val:
                with silent_operation("draw_icon_placeholder"):
                    display_name = os.path.basename(str(icon_path)) if icon_path else str(icon_val)
                    ph_w = min(48, int(rect.width() * 0.18))
                    ph_h = min(48, int(rect.height() * 0.18))
                    ph_rect = QRectF(rect.left()+8, rect.top()+8, ph_w, ph_h)
                    painter.setPen(QPen(QColor(90, 90, 90), 1))
                    painter.setBrush(QBrush(QColor(200, 200, 200)))
                    painter.drawRect(ph_rect)
                    fm = painter.fontMetrics()
                    elided = fm.elidedText(display_name, Qt.TextElideMode.ElideMiddle, int(ph_rect.width()-4))
                    painter.setPen(QColor(40,40,40))
                    painter.drawText(ph_rect.adjusted(2, 2, -2, -2), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), elided)

    def hoverEnterEvent(self, event):
        self.hovered = True
        self.update()
        # Show tooltip with focus info
        tooltip = f"ID: {self.focus.id}\nCost: {self.focus.cost} Weeks"
        if self.focus.name:
            tooltip = f"Name: {self.focus.name}\n" + tooltip
        self.setToolTip(tooltip)
        with silent_operation("start_hover_timer"):
            # start hover timer to show lineage quick-view after 1s
            self._hover_timer.start()

    def hoverMoveEvent(self, event):
        """Record the last scene position while hovering so paint() can render coords."""
        with silent_operation("store_hover_scene_pos"):
            # store scene position (QPointF)
            self._hover_scene_pos = event.scenePos()
            # request repaint to show updated coords
            self.update()

    def hoverLeaveEvent(self, event):
        self.hovered = False
        self.update()
        with silent_operation("clear_hover_scene_pos"):
            # clear stored hover scene pos
            if hasattr(self, '_hover_scene_pos'):
                delattr(self, '_hover_scene_pos')
        with silent_operation("clear_hover_scene_pos_fallback"):
            del self._hover_scene_pos
        with silent_operation("stop_hover_timer"):
            if self._hover_timer.isActive():
                self._hover_timer.stop()
        # clear lineage highlight when leaving
        with silent_operation("clear_lineage_highlight"):
            if hasattr(self.editor, 'canvas'):
                self.editor.canvas.clear_highlight()

    def _on_hover_timeout(self):
        # Trigger lineage quick-view
        with silent_operation("trigger_lineage_highlight"):
            if hasattr(self.editor, 'canvas'):
                self.editor.canvas.highlight_lineage(self.focus.id)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # Update focus grid position based on visual position
            new_pos = value
            grid_x = round(new_pos.x() / GRID_UNIT)
            grid_y = round(new_pos.y() / GRID_UNIT)
            self.focus.x = grid_x
            self.focus.y = grid_y
            # Snap to grid
            snapped_pos = QPointF(grid_x * GRID_UNIT, grid_y * GRID_UNIT)
            # Update connected lines on next event loop pass
            QTimer.singleShot(0, self.update_connections)
            # Schedule frames update (throttled) so frames follow node movement
            with silent_operation("schedule_frame_update_on_position_change"):
                if hasattr(self, 'editor') and hasattr(self.editor, 'canvas'):
                    if not getattr(self.editor.canvas, '_suspend_layout', False):
                        with silent_operation("canvas_schedule_frame_update"):
                            self.editor.canvas.schedule_frame_update()
            return snapped_pos
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.update_connections()
            # moving a node may change isolation of neighbors or itself; schedule reflow
            with silent_operation("schedule_reflow_on_position_changed"):
                if hasattr(self, 'editor') and hasattr(self.editor, 'canvas'):
                    if not getattr(self.editor.canvas, '_suspend_layout', False):
                        timer = getattr(self.editor.canvas, '_reflow_timer', None)
                        if timer and not timer.isActive():
                            timer.start()
            # update any note→focus connectors anchored to this node
            for nf in list(getattr(self, 'note_focus_connectors', [])):
                with silent_operation("update_note_focus_connector_path"):
                    nf.update_path()
            # update any event↔focus connectors anchored to this node
            for ef in list(getattr(self, 'event_focus_connectors', [])):
                with silent_operation("update_event_focus_connector_path"):
                    ef.update_path()
            # refresh spatial index bounds after movement
            with silent_operation("refresh_spatial_index_bounds"):
                canvas = getattr(self.editor, 'canvas', None)
                if canvas is not None and hasattr(canvas, '_spatial_index'):
                    canvas._spatial_index.update(self, self.sceneBoundingRect())
        # When selection changes, repaint (halo/title) and refresh connections so
        # selection visuals and anchored lines update immediately.
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # Notify scene of geometry change for proper invalidation (halo extends bounds)
            with silent_operation("prepare_geometry_change"):
                self.prepareGeometryChange()
            with silent_operation("update_on_selection_change"):
                self.update()
            with silent_operation("update_connections_on_selection"):
                self.update_connections()
            # Force scene to invalidate the item's region to clear any stale rendering
            with silent_operation("scene_invalidate_on_selection"):
                sc = self.scene()
                if sc is not None:
                    # Invalidate an expanded rect that includes the halo
                    # Use a larger invalidation margin to be robust against halo/antialiasing and
                    # viewport update heuristics that sometimes leave pixels stale.
                    expanded_rect = self.boundingRect().adjusted(-30, -30, 30, 30)
                    with silent_operation("invalidate_expanded_rect"):
                        sc.invalidate(self.mapRectToScene(expanded_rect))
                    with silent_operation("invalidate_scene_fallback"):
                        sc.invalidate()
                    # Also force the view's viewport to repaint immediately to avoid leftover artifacts
                    with silent_operation("update_viewports"):
                        views = sc.views()
                        if views:
                            for v in views:
                                with silent_operation("update_viewport"):
                                    v.viewport().update()

        # When visibility changes, ensure connection lines refresh to hide/show
        # in sync with this node.
        elif change == QGraphicsItem.GraphicsItemChange.ItemVisibleHasChanged:
            with silent_operation("update_connections_on_visibility"):
                self.update_connections()
            with silent_operation("update_visible_nodes_cache"):
                canvas = getattr(self.editor, 'canvas', None)
                if canvas is not None and hasattr(canvas, '_visible_nodes_cache'):
                    if bool(value):
                        canvas._visible_nodes_cache.add(self)
                    else:
                        canvas._visible_nodes_cache.discard(self)
        return super().itemChange(change, value)

    def update_connections(self):
        """Update all connections related to this node"""
        for connection in self.connections_in + self.connections_out:
            if hasattr(connection, 'update_path') and callable(getattr(connection, 'update_path')):
                connection.update_path()
        # update any mutual exclusion connectors anchored to this node
        for mx in getattr(self, 'mutex_connectors', []):
            with silent_operation("update_mutex_connector_path"):
                mx.update_path()

        # update any note->focus connectors anchored to this node
        for nf in getattr(self, 'note_focus_connectors', []):
            with silent_operation("update_note_focus_connector"):
                nf.update_path()

        # update any event<->focus connectors anchored to this node
        for ef in getattr(self, 'event_focus_connectors', []):
            with silent_operation("update_event_focus_connector"):
                ef.update_path()

    def mouseDoubleClickEvent(self, event):
        # Left double-click -> edit (hover shows lineage quick-view after 1s)
        if event.button() == Qt.MouseButton.LeftButton:
            self.editor.edit_focus(self.focus)

    def mousePressEvent(self, event):
        # Suspend auto-layout while the user is dragging this node
        with silent_operation("suspend_layout_on_mouse_press"):
            if event.button() == Qt.MouseButton.LeftButton and hasattr(self, 'editor') and hasattr(self.editor, 'canvas'):
                setattr(self.editor.canvas, '_suspend_layout', True)
                # capture starting scene pos for undo
                with silent_operation("capture_move_start_pos"):
                    self._move_start_scene_pos = QPointF(self.scenePos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        # Resume auto-layout after release with a slight delay to avoid jitter
        with silent_operation("resume_layout_on_mouse_release"):
            if hasattr(self, 'editor') and hasattr(self.editor, 'canvas'):
                def _resume():
                    with silent_operation("resume_layout_delayed"):
                        setattr(self.editor.canvas, '_suspend_layout', False)
                        # allow frames to refresh now
                        self.editor.canvas.schedule_frame_update()
                QTimer.singleShot(120, _resume)

    # Drag & Drop support to set icon
    def dragEnterEvent(self, event):
        with silent_operation("drag_enter_check_mime"):
            md = event.mimeData()
            if md and (md.hasUrls() or md.hasText()):
                if md.hasUrls():
                    for url in md.urls():
                        p = url.toLocalFile()
                        if p and os.path.splitext(p)[1].lower() in ('.tga', '.dds'):
                            event.acceptProposedAction()
                            return
                if md.hasText():
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        with silent_operation("drop_event_process"):
            md = event.mimeData()
            icon_val = None
            if md.hasUrls():
                # take first valid icon file
                for url in md.urls():
                    p = url.toLocalFile()
                    if p and os.path.splitext(p)[1].lower() in ('.tga', '.dds'):
                        icon_val = p
                        break
            if icon_val is None and md.hasText():
                # treat text as library key if exists; else as path
                t = md.text().strip()
                lib = getattr(self.editor, 'icon_library', {}) if hasattr(self.editor, 'icon_library') else {}
                icon_val = lib.get(t, t)
            if icon_val:
                uw = getattr(self.editor, 'undo_stack', None)
                if uw is not None:
                    uw.push(SetIconCommand(self, icon_val, description=f"Set Icon via Drop for {self.focus.id}"))
                else:
                    self.focus.icon = icon_val
                    # clear cached pixmap so it reloads
                    with silent_operation("clear_cached_icon_pixmap"):
                        if hasattr(self.focus, '_cached_icon_pixmap'):
                            delattr(self.focus, '_cached_icon_pixmap')
                    with silent_operation("clear_cached_icon_pixmap_fallback"):
                        del self.focus._cached_icon_pixmap
                    # force repaint and status update
                    self.update()
                    with silent_operation("update_status_after_drop"):
                        self.editor.update_status()
                event.acceptProposedAction()
                return
        event.ignore()

    def contextMenuEvent(self, event):
        with silent_operation("context_menu_event_outer"):
            menu = QMenu()
            # Create actions only when applicable so the menu stays relevant
            edit_action = None
            choose_icon = None
            from_lib = None
            remove_icon = None
            create_note_link = None
            unlink_child_action = None
            unlink_parent_action = None
            duplicate_action = None
            del_action = None

            # Edit properties available if parent editor supports it
            with silent_operation("add_edit_properties_action"):
                if hasattr(self, 'editor') and getattr(self.editor, 'edit_focus', None):
                    edit_action = menu.addAction("Edit Properties")

            # Icon controls: choosing an icon is generally useful if editor present
            with silent_operation("add_icon_controls"):
                if hasattr(self, 'editor'):
                    choose_icon = menu.addAction("Choose Icon (.tga/.dds)...")
                    # 'From Library' only if an icon library is present
                    with silent_operation("check_icon_library"):
                        if getattr(self.editor, 'icon_library', None):
                            from_lib = menu.addAction("Choose Icon From Library...")
                    # Remove icon only if an icon is currently set
                    with silent_operation("check_focus_icon"):
                        if getattr(self.focus, 'icon', None):
                            remove_icon = menu.addAction("Remove Icon")

            # Separator before link/delete actions if any of the following will be present
            # Create note/link if canvas is available
            canvas = None
            with silent_operation("get_canvas_scene"):
                canvas = getattr(self, 'scene', None) and getattr(self, 'scene', lambda: None)()
            main = None
            with silent_operation("get_editor_main"):
                main = getattr(self, 'editor', None)

            # Create note and link available when canvas exists
            with silent_operation("add_create_note_link_action"):
                if canvas is not None or (main is not None and getattr(main, 'canvas', None)):
                    create_note_link = menu.addAction("Create Note and Link")

            # Unlink parent/child only if node has connections
            has_children = False
            with silent_operation("check_connections_out"):
                has_children = bool(getattr(self, 'connections_out', []))
            has_parents = False
            with silent_operation("check_connections_in"):
                has_parents = bool(getattr(self, 'connections_in', []))
            with silent_operation("add_unlink_actions"):
                if has_children:
                    unlink_child_action = menu.addAction("Unlink Child")
                if has_parents:
                    unlink_parent_action = menu.addAction("Unlink Parent")

            # Duplicate and delete require an owning editor/main window
            with silent_operation("add_duplicate_delete_actions"):
                if main is not None:
                    duplicate_action = menu.addAction("Duplicate Focus")
                    del_action = menu.addAction("Delete Focus")
            # If exactly two focuses are selected, offer quick 'Make Mutually Exclusive'
            with silent_operation("setup_make_mutex_selection"):
                sel = [i for i in (self.scene().selectedItems() or []) if isinstance(i, FocusNode)]
                if len(sel) == 2:
                    # Precompute downstream clusters for both selected nodes so we
                    # can decide whether the menu item should be shown at all.
                    def collect_linked_ids(start_node):
                        ids = set()
                        stack = [start_node]
                        while stack:
                            n = stack.pop()
                            fid = None
                            with silent_operation("get_focus_id"):
                                fid = getattr(getattr(n, 'focus', None), 'id', None)
                            if not fid or fid in ids:
                                continue
                            ids.add(fid)
                            with silent_operation("traverse_connections_out"):
                                for conn in getattr(n, 'connections_out', []) or []:
                                    with silent_operation("append_end_node"):
                                        stack.append(conn.end_node)
                        return ids

                    a_node_tmp, b_node_tmp = sel[0], sel[1]
                    a_cluster_tmp = collect_linked_ids(a_node_tmp)
                    b_cluster_tmp = collect_linked_ids(b_node_tmp)

                    def _would_add_any_mutex(a_cluster, b_cluster):
                        try:
                            parent = getattr(self, 'editor', None)
                            if parent is None:
                                return True
                            focuses = getattr(parent, 'focuses', [])
                            fm = {getattr(f, 'id', None): f for f in focuses}
                            for aa in a_cluster:
                                for bb in b_cluster:
                                    if aa == bb:
                                        continue
                                    fa = fm.get(aa)
                                    if fa is None:
                                        return True
                                    me = getattr(fa, 'mutually_exclusive', None) or []
                                    if bb not in me:
                                        return True
                            return False
                        except Exception:
                            return True

                    show_make_mutex = _would_add_any_mutex(a_cluster_tmp, b_cluster_tmp)

                    def _make_mutex():
                        try:
                            # The selection 'sel' is from the outer scope of contextMenuEvent
                            a_node, b_node = sel[0], sel[1]
                            a_focus = getattr(a_node, 'focus', None)
                            b_focus = getattr(b_node, 'focus', None)

                            if not a_focus or not b_focus:
                                return

                            a_id = a_focus.id
                            b_id = b_focus.id

                            # Prefer doing this operation via the undo stack so it can be undone/redone
                            parent = getattr(self, 'editor', None)
                            uw = getattr(parent, 'undo_stack', None) if parent is not None else None
                            if uw is not None:
                                with silent_operation("push_make_mutex_command"):
                                    cmd = MakeMutexCommand(parent, a_id, b_id, description=f"Make {a_id} <-> {b_id} mutex")
                                    uw.push(cmd)
                            else:
                                # No undo stack: mutate directly (legacy behavior)
                                if getattr(a_focus, 'mutually_exclusive', None) is None:
                                    a_focus.mutually_exclusive = []
                                if b_id not in a_focus.mutually_exclusive:
                                    a_focus.mutually_exclusive.append(b_id)
                                if getattr(b_focus, 'mutually_exclusive', None) is None:
                                    b_focus.mutually_exclusive = []
                                if a_id not in b_focus.mutually_exclusive:
                                    b_focus.mutually_exclusive.append(a_id)
                                if parent is not None and hasattr(parent, '_sync_mutual_exclusive'):
                                    with silent_operation("sync_mutual_exclusive_a"):
                                        parent._sync_mutual_exclusive(a_id)
                                    with silent_operation("sync_mutual_exclusive_b"):
                                        parent._sync_mutual_exclusive(b_id)
                                if hasattr(self, 'scene') and getattr(self.scene(), 'refresh_mutex_connectors', None):
                                    self.scene().refresh_mutex_connectors()
                                if getattr(self, 'editor', None) and getattr(self.editor, 'statusBar', None):
                                    self.editor.statusBar().showMessage(f"Made {a_id} and {b_id} mutually exclusive.", 4000)

                        except Exception as e:
                            # Use a logger if available, otherwise print
                            try:
                                logger.error("Error in _make_mutex: %s", e, exc_info=True)
                            except NameError:
                                print(f"Error in _make_mutex: {e}")
                    if show_make_mutex:
                        menu.addAction("Make Mutually Exclusive").triggered.connect(_make_mutex)
            with silent_operation("setup_make_mutex_action"):
                pass
            try:
                from PyQt6.QtGui import QCursor
                act = menu.exec(QCursor.pos())
            except Exception:
                act = menu.exec(event.screenPos())
            if act is unlink_child_action or act is unlink_parent_action:
                with silent_operation("handle_unlink_action_outer"):
                    sc = self.scene()
                    if sc is None:
                        raise Exception('no scene')
                    # Prefer targeted removal helpers if available
                    if act is unlink_child_action and hasattr(sc, 'remove_focus_child_links_for'):
                        with silent_operation("remove_focus_child_links"):
                            sc.remove_focus_child_links_for(self)
                    elif act is unlink_parent_action and hasattr(sc, 'remove_focus_parent_links_for'):
                        with silent_operation("remove_focus_parent_links"):
                            sc.remove_focus_parent_links_for(self)
                    else:
                        # fallback to generic removal of non-prereq links
                        if hasattr(sc, 'remove_links_for'):
                            with silent_operation("remove_links_fallback"):
                                sc.remove_links_for(self)
                with silent_operation("handle_unlink_action"):
                    pass
            if act == edit_action:
                self.editor.edit_focus(self.focus)
            elif act == choose_icon:
                fn, _ = QFileDialog.getOpenFileName(None, "Choose Icon", os.getcwd(), "Icons (*.tga *.dds)")
                if fn:
                    self.focus.icon = fn
                    # clear cached pixmap so it reloads
                    with silent_operation("clear_cached_icon_pixmap"):
                        if hasattr(self.focus, '_cached_icon_pixmap'):
                            delattr(self.focus, '_cached_icon_pixmap')
                    with silent_operation("delete_cached_icon_pixmap"):
                        del self.focus._cached_icon_pixmap
                    self.update()
            elif act == from_lib:
                with silent_operation("icon_library_dialog"):
                    lib = getattr(self.editor, 'icon_library', {})
                    dlg = IconLibraryDialog(lib, parent=None)
                    if dlg.exec() == QDialog.DialogCode.Accepted and getattr(dlg, 'selected', None):
                        self.focus.icon = dlg.selected
                        with silent_operation("clear_lib_icon_cache"):
                            if hasattr(self.focus, '_cached_icon_pixmap'):
                                delattr(self.focus, '_cached_icon_pixmap')
                        with silent_operation("delete_lib_icon_cache"):
                            del self.focus._cached_icon_pixmap
                        self.update()
            elif act == remove_icon:
                uw = getattr(self.editor, 'undo_stack', None)
                if uw is not None:
                    uw.push(SetIconCommand(self, None, description=f"Remove Icon for {self.focus.id}"))
                else:
                    with silent_operation("remove_cached_icon_pixmap"):
                        if hasattr(self.focus, '_cached_icon_pixmap'):
                            delattr(self.focus, '_cached_icon_pixmap')
                    with silent_operation("remove_cached_icon_pixmap_fallback"):
                        del self.focus._cached_icon_pixmap
                    self.focus.icon = None
                    self.update()
            elif act == create_note_link:
                with silent_operation("create_note_link_action"):
                    canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
                    main = getattr(self, 'editor', None)
                    if canvas is not None:
                        canvas.notes_enabled = True
                        note_pos = self.scenePos() + QPointF(200.0, -100.0)
                        with silent_operation("compute_note_position"):
                            center = self.sceneBoundingRect().center()
                            note_pos = QPointF(center.x() + 200.0, center.y() - 100.0)
                        # If undo stack present, create an undoable command
                        uw = getattr(main, 'undo_stack', None)
                        with silent_operation("create_note_command"):
                            cmd = NoteCreateLinkCommand(main, f"Note for {self.focus.id}", note_pos, self)
                            if uw is not None:
                                uw.push(cmd)
                            else:
                                # immediate apply
                                cmd.redo()
                        with silent_operation("fallback_note_creation"):
                            # fallback immediate creation
                            note = canvas._create_note_item(f"Note for {self.focus.id}", note_pos)
                            note.set_visible(canvas.notes_enabled)
                            with silent_operation("append_notes_items"):
                                canvas._notes_items.append(note)
                            with silent_operation("store_note_by_id"):
                                canvas._notes_by_id[note.note_id] = note
                            canvas.add_note_focus_link(note, self)
                            with silent_operation("select_new_note"):
                                note.setSelected(True)
            elif act == duplicate_action:
                self.duplicate_focus()
            elif act == del_action:
                self.editor.delete_focus_node(self)
        with silent_operation("context_menu_event"):
            pass

    def delete_focus(self):
        """Delete this focus node"""
        reply = QMessageBox.question(self.editor, "Delete Focus",
                                     f"Are you sure you want to delete focus '{self.focus.id}'?\n"
                                     f"This will also remove all connections to this focus.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.editor.delete_focus_node(self)

    def duplicate_focus(self):
        """Create a duplicate of this focus"""
        self.editor.duplicate_focus(self.focus)

# endregion

# region Event Node

class EventNode(QGraphicsItem): # event node (off-grid Ctrl move)
    """Visual representation of an event. Ctrl+drag allows free off-grid placement.
    Without Ctrl, it snaps to grid and updates event.x/y.
    """
    def __init__(self, event: Event, editor):
        super().__init__()
        self.event = event
        self.editor = editor
        with silent_operation("init_event_node"):
            with silent_operation("init_node_base"):
                NodeBase.init_node(self, editor, movable=True, selectable=True, accept_drops=False)
            with silent_operation("init_node_fallback"):
                self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
                self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
                self.setAcceptHoverEvents(True)
                self._move_start_scene_pos = None
        # cache sizes
        self.W = 220.0
        self.H = 100.0
        # Cross-type connectors (for updates on move):
        self.note_event_connectors = []
        self.event_focus_connectors = []
        self.event_event_connectors = []
        # cache for computed artwork mask path (QPainterPath), keyed to
        # the background/overlay path and ssaa in use
        self._country_art_mask_path = None
        self._country_art_mask_for = None

    def boundingRect(self):
        # When country event mode is active and a background/overlay pixmap
        # has been loaded, report the bounding rect matching the artwork's
        # non-transparent pixel extents so the item is hit-testable at the
        # actual image content bounds.
        with silent_operation("bounding_rect_country_mode_check"):
            canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
            if canvas is not None and getattr(canvas, 'country_event_mode', False):
                # attempt to compute or reuse an artwork mask path
                with silent_operation("compute_artwork_mask_bounds"):
                    self._ensure_country_art_mask()
                    if self._country_art_mask_path is not None:
                        return self._country_art_mask_path.boundingRect()
        # Include halo margin so Qt properly invalidates selection artifacts
        m = 10.0  # halo is drawn at 6px, add margin for antialiasing
        return QRectF(-self.W/2.0 - m, -self.H/2.0 - m, self.W + 2*m, self.H + 2*m)

    def shape(self):
        # Ensure mouse hit-tests follow the artwork bounds when country mode
        # is active. Fall back to a rounded rect shape otherwise.
        # Note: Use actual node bounds, NOT boundingRect() which includes halo margin.
        path = QPainterPath()
        canvas = None
        with silent_operation("shape_get_canvas"):
            canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
            if canvas is not None and getattr(canvas, 'country_event_mode', False):
                with silent_operation("shape_artwork_mask"):
                    self._ensure_country_art_mask()
                    if self._country_art_mask_path is not None:
                        return QPainterPath(self._country_art_mask_path)
        # default: rounded rect matching actual node bounds (without halo margin)
        node_rect = QRectF(-self.W/2.0, -self.H/2.0, self.W, self.H)
        path.addRoundedRect(node_rect, 10, 10)
        return path

    def _ensure_country_art_mask(self):
        """Compute and cache a QPainterPath mask of non-transparent pixels
        for the background and overlay pixmaps. The returned path is
        centered around the item origin (so it can be used directly in
        painting/shape/boundingRect). Keyed by (bg_path, ov_path, ssaa).
        """
        with silent_operation("ensure_country_art_mask_outer"):
            canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
            if canvas is None or not getattr(canvas, 'country_event_mode', False):
                return

            base_dir = os.path.dirname(__file__)
            default_bg = os.path.join(base_dir, '_assets', 'country_event_bg.png')
            default_ov = os.path.join(base_dir, '_assets', 'country_event_overlay.png')
            # Prefer explicit canvas parent/user path if set; otherwise use packaged assets.
            bg_path = getattr(canvas, 'country_event_bg_path', None) or getattr(canvas, 'country_event_bg', None) or default_bg
            ov_path = getattr(canvas, 'country_event_overlay_path', None) or getattr(canvas, 'country_event_overlay', None) or default_ov
            ssaa = float(getattr(canvas, 'country_event_ssaa', getattr(canvas, 'event_supersample_scale', 1.0)) or 1.0)

            key = (str(bg_path or ''), str(ov_path or ''), float(ssaa))
            if getattr(self, '_country_art_mask_for', None) == key and getattr(self, '_country_art_mask_path', None) is not None:
                return

            def _load_and_scale(path):
                # Attempt to load the image. If the path points to a packaged
                # asset inside the application's `_assets` folder, load it from
                # there. If it's an explicit user path, prefer that. If the
                # path is missing, return None.
                if not path:
                    return None
                try:
                    # If path points inside our package assets, load directly
                    if isinstance(path, str) and os.path.basename(path) in ('country_event_bg.png', 'country_event_overlay.png'):
                        packaged = os.path.join(os.path.dirname(__file__), '_assets', os.path.basename(path))
                        if os.path.exists(packaged):
                            pm = QPixmap(packaged)
                        else:
                            # try the provided path if it exists
                            pm = QPixmap(path) if os.path.exists(path) else None
                    else:
                        pm = QPixmap(path) if os.path.exists(path) else None
                    if ssaa != 1.0:
                        sw = int(max(1, round(pm.width() * ssaa)))
                        sh = int(max(1, round(pm.height() * ssaa)))
                        sp = pm.scaled(sw, sh, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                        with silent_operation("set_device_pixel_ratio"):
                            sp.setDevicePixelRatio(ssaa)
                        return sp
                    else:
                        return pm
                except Exception:
                    return None

            bg_pm = _load_and_scale(bg_path)
            ov_pm = _load_and_scale(ov_path)

            def _path_from_pixmap(pm):
                if pm is None or pm.isNull():
                    return None
                try:
                    img = pm.toImage()
                    dpr = 1.0
                    with silent_operation("get_device_pixel_ratio"):
                        dpr = float(getattr(pm, 'devicePixelRatio', lambda: 1.0)() or 1.0)
                    pw = img.width()
                    ph = img.height()
                    logical_w = float(pw) / (dpr if dpr else 1.0)
                    logical_h = float(ph) / (dpr if dpr else 1.0)
                    p = QPainterPath()
                    # Build path using horizontal spans per row for non-transparent pixels
                    for yy in range(ph):
                        xx = 0
                        while xx < pw:
                            # skip transparent pixels
                            while xx < pw and img.pixelColor(xx, yy).alpha() == 0:
                                xx += 1
                            if xx >= pw:
                                break
                            x0 = xx
                            while xx < pw and img.pixelColor(xx, yy).alpha() != 0:
                                xx += 1
                            x1 = xx
                            # rect in logical coords, centered around item origin
                            rx = float(x0) / (dpr if dpr else 1.0) - logical_w/2.0
                            ry = float(yy) / (dpr if dpr else 1.0) - logical_h/2.0
                            rw = float(x1 - x0) / (dpr if dpr else 1.0)
                            rh = 1.0 / (dpr if dpr else 1.0)
                            p.addRect(QRectF(rx, ry, rw, rh))
                    return p
                except Exception:
                    return None

            path_bg = _path_from_pixmap(bg_pm)
            path_ov = _path_from_pixmap(ov_pm)
            final = None
            if path_bg is not None and path_ov is not None:
                try:
                    final = path_bg.united(path_ov)
                except Exception:
                    final = QPainterPath(path_bg)
                    final.addPath(path_ov)
            elif path_bg is not None:
                final = path_bg
            elif path_ov is not None:
                final = path_ov

            if final is None:
                with silent_operation("fallback_mask_rect"):
                    if bg_pm is not None and not bg_pm.isNull():
                        dpr = 1.0
                        with silent_operation("get_bg_device_pixel_ratio"):
                            dpr = float(getattr(bg_pm, 'devicePixelRatio', lambda: 1.0)() or 1.0)
                        w = float(bg_pm.width()) / (dpr if dpr else 1.0)
                        h = float(bg_pm.height()) / (dpr if dpr else 1.0)
                        p = QPainterPath()
                        p.addRect(QRectF(-w/2.0, -h/2.0, w, h))
                        final = p

            self._country_art_mask_path = final
            self._country_art_mask_for = key
        with silent_operation("ensure_country_art_mask"):
            pass

    def paint(self, painter, option, widget):
        rect = self.boundingRect()
        # Determine canvas and tint/brush (use same override map as focus nodes)
        canvas = getattr(self.editor, 'canvas', None) if hasattr(self, 'editor') else None
        tint = None
        with silent_operation("get_event_tint_color"):
            if canvas is not None:
                override = getattr(canvas, 'focus_color_overrides', {}).get(self.event.id) if getattr(canvas, 'focus_color_overrides', None) else None
                if override is not None:
                    tint = QColor(override)
                else:
                    # fallback to lineage/network coloring using same maps as focus
                    lid = getattr(canvas, '_lineage_of_node', {}).get(self.event.id) if getattr(canvas, '_lineage_of_node', None) else None
                    if lid and lid in getattr(canvas, '_lineage_colors', {}):
                        tint = QColor(canvas._lineage_colors[lid])
                    else:
                        net = getattr(self.event, 'network_id', None)
                        if net is not None and net in getattr(canvas, 'network_colors', {}):
                            tint = QColor(canvas.network_colors[net])
            if tint is None and canvas is not None and getattr(canvas, 'default_focus_color', None):
                tint = QColor(canvas.default_focus_color)

        # style distinct from focus: rounded pill; allow tint to change background
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        border = QColor(60, 110, 160)
        if tint is None:
            bg = QColor(190, 220, 255)
        else:
            bg = QColor(190, 220, 255)
            with silent_operation("compute_tint_bg_color"):
                h, s, l, a = tint.getHslF()
                bg = QColor()
                bg.setHslF(h, min(0.35, max(0.15, s*0.4)), min(0.85, max(0.6, l*1.05)), 1.0)
        # Selection / hover halo for non-country mode; country mode will draw a
        # tight artwork-aligned outline later after the artwork is placed.
        with silent_operation("draw_selection_hover_halo"):
            if not getattr(canvas, 'country_event_mode', False):
                halo = None
                if self.isSelected():
                    halo = QColor(255, 120, 120, 90)
                elif getattr(self, 'hovered', False):
                    halo = QColor(80, 120, 220, 60)
                if halo is not None:
                    painter.save()
                    try:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QBrush(halo))
                        painter.drawRoundedRect(rect.adjusted(-6, -6, 6, 6), 12, 12)
                    finally:
                        painter.restore()

        pen_w = 4 if self.isSelected() else 2
        # If not in country event mode, draw the usual rounded pill background.
        # For country mode we intentionally do not draw this extra decoration so
        # the artwork appears at native resolution with no additional framing.
        country_mode_active = bool(canvas is not None and getattr(canvas, 'country_event_mode', False))
        if not country_mode_active:
            painter.setPen(QPen(border, pen_w))
            painter.setBrush(QBrush(bg))
            painter.drawRoundedRect(rect, 10, 10)
        # Country Event rendering mode (optional) — uses two supplied assets and
        # renders title, description and option(s) at native resolution while
        # preserving SSAA. If the canvas does not request this mode we fall back
        # to the regular compact title/id layout below.
        title = self.event.title or self.event.id
        try:
            if canvas is not None and getattr(canvas, 'country_event_mode', False):
                # Allow the canvas to override asset paths. If not provided,
                # fall back to the packaged assets in `_assets/`.
                default_bg = default_ov = None
                with silent_operation("get_default_asset_paths"):
                    base_dir = os.path.dirname(__file__)
                    default_bg = os.path.join(base_dir, '_assets', 'country_event_bg.png')
                    default_ov = os.path.join(base_dir, '_assets', 'country_event_overlay.png')

                bg_path = getattr(canvas, 'country_event_bg_path', None) or getattr(canvas, 'country_event_bg', None) or default_bg
                ov_path = getattr(canvas, 'country_event_overlay_path', None) or getattr(canvas, 'country_event_overlay', None) or default_ov

                def _load_pix_cached(attr, path):
                    pm = getattr(self, attr, None)
                    # If not cached, try to load either packaged asset or user path
                    if pm is None and path:
                        try:
                            # If the path equals the packaged defaults, prefer the packaged file
                            bname = os.path.basename(path) if isinstance(path, str) else ''
                            if bname in ('country_event_bg.png', 'country_event_overlay.png'):
                                packaged = os.path.join(os.path.dirname(__file__), '_assets', bname)
                                if os.path.exists(packaged):
                                    pm = QPixmap(packaged)
                                elif os.path.exists(path):
                                    pm = QPixmap(path)
                                else:
                                    pm = None
                            else:
                                pm = QPixmap(path) if os.path.exists(path) else None
                            if pm is not None:
                                with silent_operation("set_pixmap_path_attr"):
                                    setattr(pm, 'path', path)
                                setattr(self, attr, pm)
                        except Exception:
                            pm = None
                    return pm

                bg_pm = _load_pix_cached('_country_bg_pixmap', bg_path)
                ov_pm = _load_pix_cached('_country_ov_pixmap', ov_path)

                # Supersample scale for the image stack (background + overlay).
                # Read the dedicated setting first, fall back to older name for
                # compatibility.
                ssaa = float(getattr(canvas, 'country_event_ssaa', getattr(canvas, 'event_supersample_scale', 1.0)) or 1.0)

                logical_bg_w = logical_bg_h = None
                if bg_pm and not bg_pm.isNull():
                    # Create a supersampled pixmap for crisp rendering when scaled
                    if ssaa != 1.0:
                        sp = bg_pm
                        with silent_operation("scale_bg_pixmap"):
                            sw = int(max(1, round(bg_pm.width() * ssaa)))
                            sh = int(max(1, round(bg_pm.height() * ssaa)))
                            sp = bg_pm.scaled(sw, sh, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                            with silent_operation("set_bg_dpr"):
                                sp.setDevicePixelRatio(ssaa)
                    else:
                        sp = bg_pm
                    dpr = 1.0
                    with silent_operation("get_bg_dpr"):
                        dpr = float(getattr(sp, 'devicePixelRatio', lambda: 1.0)() or 1.0)
                    logical_bg_w = float(sp.width()) / (dpr if dpr else 1.0)
                    logical_bg_h = float(sp.height()) / (dpr if dpr else 1.0)

                    # center the background inside the node rect
                    bx = rect.center().x() - logical_bg_w / 2.0
                    by = rect.center().y() - logical_bg_h / 2.0
                    painter.save()
                    try:
                        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                        painter.drawPixmap(QPointF(bx, by), sp)
                    finally:
                        painter.restore()

                if ov_pm and not ov_pm.isNull():
                    # Draw overlay on top of the background. Respect SSAA as above.
                    if ssaa != 1.0:
                        sp_ov = ov_pm
                        with silent_operation("scale_ov_pixmap"):
                            sw = int(max(1, round(ov_pm.width() * ssaa)))
                            sh = int(max(1, round(ov_pm.height() * ssaa)))
                            sp_ov = ov_pm.scaled(sw, sh, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                            with silent_operation("set_ov_dpr"):
                                sp_ov.setDevicePixelRatio(ssaa)
                    else:
                        sp_ov = ov_pm
                    dpr_ov = 1.0
                    with silent_operation("get_ov_dpr"):
                        dpr_ov = float(getattr(sp_ov, 'devicePixelRatio', lambda: 1.0)() or 1.0)
                    logical_ov_w = float(sp_ov.width()) / (dpr_ov if dpr_ov else 1.0)
                    logical_ov_h = float(sp_ov.height()) / (dpr_ov if dpr_ov else 1.0)

                    # center overlay over the background if present, otherwise center in rect
                    if logical_bg_w and logical_bg_h:
                        ox = rect.center().x() - logical_ov_w / 2.0
                        oy = rect.center().y() - logical_ov_h / 2.0
                    else:
                        ox = rect.center().x() - logical_ov_w / 2.0
                        oy = rect.center().y() - logical_ov_h / 2.0
                    painter.save()
                    try:
                        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                        painter.drawPixmap(QPointF(ox, oy), sp_ov)
                    finally:
                        painter.restore()

                # Ensure artwork mask exists and, in country mode, clip to it so
                # no rounded-pill background or text spills outside the image.
                with silent_operation("ensure_country_art_mask_paint"):
                    self._ensure_country_art_mask()

                # Now render text (vector) on top of the artwork. Use word-wrapping
                # and keep sizes relative to the node rectangle so layout is stable.
                try:
                    # If a mask path is available and we're in country mode, clip
                    # subsequent drawing operations to the mask so only non-alpha
                    # pixels are considered visible.
                    painter.save()
                    mask_path = getattr(self, '_country_art_mask_path', None) if country_mode_active else None
                    if country_mode_active and mask_path is not None:
                        with silent_operation("set_clip_path"):
                            painter.setClipPath(mask_path)
                    # Prepare bundled AFL font (if present) for later use.
                    fam = None
                    with silent_operation("load_embedded_font"):
                        # Prefer embedded font data when available so the app
                        # can ship the TTF without relying on external files.
                        from _embedded_fonts import get_afl_font_family
                        from PyQt6.QtGui import QFont
                        fam = get_afl_font_family()

                    # Reserve title rect (title is drawn after the clip so it
                    # remains visible) — description and options depend on
                    # the same title_rect for layout.
                    # Respect canvas-configured event title offsets
                    et_off_x = 0
                    with silent_operation("get_title_offset_x"):
                        et_off_x = int(getattr(canvas, 'event_title_offset_x', 0))
                    et_off_y = 0
                    with silent_operation("get_title_offset_y"):
                        et_off_y = int(getattr(canvas, 'event_title_offset_y', 0))
                    title_rect = QRectF(rect.left()+12 + et_off_x, rect.top()+8 + et_off_y, rect.width()-24, rect.height()*0.18)

                    # Determine base font family for event text (prefer bundled AFL if loaded)
                    base_family = painter.font().family()
                    with silent_operation("get_base_font_family"):
                        base_family = fam or painter.font().family()

                    # Description (main body) — allow canvas override for font size
                    default_desc_size = 9
                    with silent_operation("get_default_desc_size"):
                        default_desc_size = max(8, painter.font().pointSize() - 1)
                    desc_size = default_desc_size
                    with silent_operation("get_desc_font_size"):
                        desc_size = int(getattr(canvas, 'event_desc_font_size', default_desc_size))
                    df = painter.font(); df.setBold(False); df.setPointSize(max(8, df.pointSize()-1))
                    with silent_operation("create_desc_font"):
                        from PyQt6.QtGui import QFont
                        df = QFont(base_family, max(6, int(desc_size)))
                        df.setBold(False)
                    painter.setFont(df)
                    desc = getattr(self.event, 'description', '') or ''
                    # Description rect (also accepts an optional canvas offset)
                    ed_off_x = 0
                    with silent_operation("get_desc_offset_x"):
                        ed_off_x = int(getattr(canvas, 'event_desc_offset_x', 0))
                    ed_off_y = 0
                    with silent_operation("get_desc_offset_y"):
                        ed_off_y = int(getattr(canvas, 'event_desc_offset_y', 0))
                    desc_rect = QRectF(rect.left()+12 + ed_off_x, title_rect.bottom()+6 + ed_off_y, rect.width()-24, rect.height()*0.54)
                    # Description text should be black for readability over artwork in country event mode
                    desc_col = QColor(0, 0, 0) if country_mode_active else QColor(40, 60, 80)
                    painter.setPen(desc_col)
                    painter.drawText(desc_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap), desc)

                    # Options: prefer a localized option label instead of raw
                    # options_block text. Compute a primary option key (a/b/...)
                    # then prefer per-event localisation values (event.option_loc_values)
                    # or a primary_loc_value fallback. Draw the label centered over
                    # the overlay bar if its position is known, otherwise center
                    # inside the options area.
                    opts_block = getattr(self.event, 'options_block', '') or ''
                    eo_off_x = 0
                    with silent_operation("get_options_offset_x"):
                        eo_off_x = int(getattr(canvas, 'event_options_offset_x', 0))
                    eo_off_y = 0
                    with silent_operation("get_options_offset_y"):
                        eo_off_y = int(getattr(canvas, 'event_options_offset_y', 0))
                    opt_rect = QRectF(rect.left()+12 + eo_off_x, desc_rect.bottom()+6 + eo_off_y, rect.width()-24, rect.bottom()-desc_rect.bottom()-8)
                    # Options font — allow canvas override and keep italic style
                    default_opt_size = 8
                    with silent_operation("get_default_opt_size"):
                        default_opt_size = max(8, painter.font().pointSize() - 2)
                    opt_size = default_opt_size
                    with silent_operation("get_opt_font_size"):
                        opt_size = int(getattr(canvas, 'event_options_font_size', default_opt_size))
                    of = painter.font(); of.setItalic(True); of.setPointSize(max(8, of.pointSize()-2))
                    with silent_operation("create_opt_font"):
                        from PyQt6.QtGui import QFont
                        of = QFont(base_family, max(6, int(opt_size)))
                        of.setItalic(True)
                    painter.setFont(of)
                    # Option text should be white for country event rendering to contrast artwork
                    pen_col = QColor(255, 255, 255) if country_mode_active else QColor(60, 60, 60)
                    painter.setPen(pen_col)

                    # Determine primary option key. Prefer explicit per-event
                    # option_keys if present (ordered list), then editor helper,
                    # then fallbacks parsing options_block.
                    primary_key = None
                    with silent_operation("get_explicit_option_keys"):
                        # first prefer explicit event.option_keys list
                        ev_keys = getattr(self.event, 'option_keys', None)
                        if isinstance(ev_keys, (list, tuple)) and ev_keys:
                            primary_key = ev_keys[0]
                    if not primary_key:
                        with silent_operation("compute_option_keys_from_editor"):
                            if getattr(self, 'editor', None) is not None and hasattr(self.editor, 'compute_event_option_keys'):
                                keys = self.editor.compute_event_option_keys(self.event)
                                if keys:
                                    primary_key = keys[0]

                    if not primary_key:
                        with silent_operation("parse_option_name_from_block"):
                            import re as _re
                            found = _re.findall(r"name\s*=\s*([A-Za-z0-9_.\-\"']+)", opts_block)
                            if found:
                                t = found[0].strip()
                                if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                                    t = t[1:-1]
                                if t:
                                    primary_key = t

                    if not primary_key:
                        count = 0
                        with silent_operation("count_options_in_block"):
                            import re as _re
                            count = len(_re.findall(r"(^|\n)\s*option\s*=", opts_block, flags=_re.IGNORECASE))
                        if count <= 0:
                            count = 1
                        # generate fallback keys like 'event.id.a', 'event.id.b', ...
                        evid = 'event.1'
                        with silent_operation("get_event_id_for_fallback"):
                            evid = str(getattr(self.event, 'id', '') or '').strip() or 'event.1'
                        i = 0
                        if count >= 1:
                            if i < 26:
                                suffix = chr(ord('a') + i)
                            else:
                                suffix = str(i+1)
                            primary_key = f"{evid}.{suffix}"

                    # Choose display text
                    # Determine the text to render: prefer per-event option_loc_values
                    # mapping (localized strings) for the selected primary_key. If not
                    # found, fall back to primary_loc_value then to primary_key string.
                    txt = str(primary_key or '')
                    with silent_operation("get_localized_option_text"):
                        vals = getattr(self.event, 'option_loc_values', None) or {}
                        if isinstance(vals, dict) and primary_key in vals and str(vals[primary_key]).strip():
                            txt = str(vals[primary_key]).strip()
                        else:
                            pv = getattr(self.event, 'primary_loc_value', None)
                            if pv and str(pv).strip():
                                txt = str(pv).strip()

                    # Draw centered over overlay if overlay coords/size are available
                    with silent_operation("draw_option_text"):
                        # prefer AFL font for the overlay label too when available
                        with silent_operation("set_afl_overlay_font"):
                            if fam:
                                ofont = QFont(fam, max(8, painter.font().pointSize()-2))
                                ofont.setItalic(True)
                                painter.setFont(ofont)
                        fm = painter.fontMetrics()
                        # prefer overlay coords if present (ox, oy, logical_ov_w, logical_ov_h)
                        has_overlay = all(v in locals() for v in ('ox', 'oy', 'logical_ov_w', 'logical_ov_h'))
                        if has_overlay and ox is not None and oy is not None and logical_ov_w and logical_ov_h:
                            ovw = float(max(1.0, fm.horizontalAdvance(txt) + 8.0))
                            with silent_operation("compute_overlay_text_width"):
                                ovw = min(float(logical_ov_w), float(max(1.0, fm.horizontalAdvance(txt) + 8.0)))
                            ovh = float(fm.height())
                            # center in overlay, then nudge up 8px and shift right by bg/3 when available
                            base_tx = float(ox) + (float(logical_ov_w) - ovw) / 2.0
                            shift_right = 0.0
                            with silent_operation("compute_shift_right_overlay"):
                                shift_right = float(logical_bg_w) / 3.0 if 'logical_bg_w' in locals() and logical_bg_w else 0.0
                            tx = base_tx + shift_right
                            ty = float(oy) + (float(logical_ov_h) - ovh) / 2.0 - 8.0
                            text_rect = QRectF(tx, ty, ovw, ovh)
                        else:
                            # center inside opt_rect
                            w_needed = float(fm.horizontalAdvance(txt) + 8.0)
                            with silent_operation("compute_opt_rect_text_width"):
                                w_needed = float(max(1.0, fm.horizontalAdvance(txt) + 8.0))
                            w_use = min(opt_rect.width(), w_needed)
                            base_tx = opt_rect.left() + (opt_rect.width() - w_use) / 2.0
                            shift_right = 0.0
                            with silent_operation("compute_shift_right_opt_rect"):
                                shift_right = float(logical_bg_w) / 3.0 if 'logical_bg_w' in locals() and logical_bg_w else 0.0
                            tx = base_tx + shift_right
                            ty = opt_rect.top() - 8.0
                            text_rect = QRectF(tx, ty, w_use, float(fm.height()))
                        painter.save()
                        try:
                            painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine), txt)
                        finally:
                            painter.restore()
                    with silent_operation("fallback_draw_opts_block"):
                        # final fallback: draw raw options_block (very small)
                        painter.drawText(opt_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap), opts_block)
                finally:
                    painter.restore()

                # After restoring the painter clip, draw the title on top of
                # the artwork so it remains visible regardless of the mask.
                try:
                    title_rect = QRectF(rect.left()+12, rect.top()+8, rect.width()-24, rect.height()*0.18)
                    try:
                        from PyQt6.QtGui import QFont
                        try:
                            default_title_size = max(10, painter.font().pointSize() + 1)
                        except Exception:
                            default_title_size = 12
                        title_size = int(getattr(canvas, 'event_title_font_size', default_title_size))
                        if fam:
                            tf = QFont(fam, max(10, int(title_size)))
                            tf.setBold(True)
                        else:
                            tf = QFont(base_family, max(10, int(title_size)))
                            tf.setBold(True)
                    except Exception:
                        tf = painter.font(); tf.setBold(True); tf.setPointSize(max(10, tf.pointSize()+1))
                    # Title backdrop intentionally removed to avoid obscuring artwork.
                    # (Previously drew a semi-opaque rounded rect behind the title.)
                    # No backdrop is drawn here so the title sits directly on the artwork.
                    # Leave empty to preserve layout.
                    painter.setFont(tf)
                    # Title should be white to contrast artwork when in country event mode
                    try:
                        # Title should be black to contrast artwork
                        title_col = QColor(0, 0, 0)
                    except Exception:
                        title_col = QColor(255, 255, 255) if country_mode_active else QColor(0, 0, 0)
                    painter.setPen(title_col)
                    # Center title horizontally (and vertically) but nudge the
                    # text itself slightly downward so it sits visually lower
                    # over the artwork while keeping title_rect for layout.
                    try:
                        # Measure the title and center a narrow rect around the
                        # node center so the title expands outward (left/right)
                        # as it grows. If the title is wider than the available
                        # title_rect, fall back to using the full width which
                        # allows word-wrapping.
                        try:
                            title_nudge = float(getattr(canvas, 'country_event_title_offset', 10.0))
                        except Exception:
                            title_nudge = 10.0
                        fm = painter.fontMetrics()
                        needed_w = float(fm.horizontalAdvance(title) + 8)
                        max_w = float(title_rect.width())
                        if needed_w < max_w:
                            w_use = needed_w
                        else:
                            # allow full width (word-wrap) when text is long
                            w_use = max_w
                        cx = title_rect.left() + title_rect.width() / 2.0
                        tx = cx - w_use / 2.0
                        text_rect = QRectF(tx, title_rect.top() + title_nudge, w_use, max(1.0, title_rect.height() - title_nudge))
                        align = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap)
                        painter.drawText(text_rect, align, title)
                    except Exception:
                        # Fallback: center using the full title_rect
                        painter.drawText(title_rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), title)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Draw a tight selection/hover outline matching the artwork
                # bounding box (prefer union of background and overlay if
                # present). This avoids drawing any extra glow or padding and
                # ensures selection highlights the image itself.
                try:
                    art_rect = None
                    try:
                        # preferred: background bounds
                        if logical_bg_w and logical_bg_h:
                            bg_rect = QRectF(bx, by, logical_bg_w, logical_bg_h)
                        else:
                            bg_rect = None
                    except Exception:
                        bg_rect = None
                    try:
                        if 'logical_ov_w' in locals() and logical_ov_w and logical_ov_h:
                            ov_rect = QRectF(ox, oy, logical_ov_w, logical_ov_h)
                        else:
                            ov_rect = None
                    except Exception:
                        ov_rect = None
                    if bg_rect and ov_rect:
                        # union of both rects
                        art_rect = bg_rect.united(ov_rect)
                    elif bg_rect:
                        art_rect = bg_rect
                    elif ov_rect:
                        art_rect = ov_rect
                    else:
                        art_rect = QRectF(rect)

                    if art_rect is not None and (self.isSelected() or getattr(self, 'hovered', False)):
                        sel_color = QColor(255, 120, 120) if self.isSelected() else QColor(80, 120, 220)
                        pen_w = 3 if self.isSelected() else 2
                        painter.save()
                        try:
                            painter.setBrush(Qt.BrushStyle.NoBrush)
                            painter.setPen(QPen(sel_color, pen_w, Qt.PenStyle.SolidLine, Qt.PenJoinStyle.BevelJoin))
                            mask_path = getattr(self, '_country_art_mask_path', None)
                            if mask_path is not None:
                                try:
                                    # Stroke the mask path itself for an accurate outline
                                    painter.drawPath(mask_path)
                                except Exception:
                                    # Fallback to rect outline if path drawing fails
                                    painter.drawRect(art_rect)
                            else:
                                painter.drawRect(art_rect)
                        finally:
                            painter.restore()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                # Completed custom country rendering; don't fall-through to default
                return
        except Exception:
            # If anything fails, fall back to the compact display below
            pass

        # title (fallback compact rendering) - prefer bundled AFL font when available
        try:
            # Use embedded font if available (falls back to system fonts)
            from _embedded_fonts import get_afl_font_family
            from PyQt6.QtGui import QFont
            fam = get_afl_font_family()
        except Exception:
            fam = None
        painter.setPen(QColor(0, 0, 0))
        try:
            if fam:
                f = QFont(fam, painter.font().pointSize())
                f.setBold(True)
            else:
                f = painter.font(); f.setBold(True)
            painter.setFont(f)
        except Exception:
            f = painter.font(); f.setBold(True); painter.setFont(f)
        painter.drawText(rect.adjusted(8, 6, -8, -rect.height()/2+14), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        # id small
        f2 = painter.font(); f2.setBold(False); f2.setPointSize(max(8, f2.pointSize()-1)); painter.setFont(f2)
        painter.setPen(QColor(40, 60, 80))
        try:
            scene_obj = self.scene() if hasattr(self, 'scene') else None
        except Exception:
            scene_obj = None
        if getattr(scene_obj, 'render_node_ids', True):
            painter.drawText(rect.adjusted(8, rect.height()/2-24, -8, -6), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom), self.event.id)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            new_pos = value
            # If Ctrl is pressed, allow free placement; store free_x/free_y in scene coordinates
            ctrl_down = False
            try:
                mods = QApplication.keyboardModifiers()
                ctrl_down = bool(mods & Qt.KeyboardModifier.ControlModifier)
            except Exception:
                ctrl_down = False
            if ctrl_down:
                try:
                    self.event.free_x = float(new_pos.x())
                    self.event.free_y = float(new_pos.y())
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # do not snap
                QTimer.singleShot(0, self.update)
                return new_pos
            # snap to grid
            grid_x = round(new_pos.x() / GRID_UNIT)
            grid_y = round(new_pos.y() / GRID_UNIT)
            self.event.x = int(grid_x)
            self.event.y = int(grid_y)
            # clear free placement when snapping
            self.event.free_x = None
            self.event.free_y = None
            snapped = QPointF(grid_x * GRID_UNIT, grid_y * GRID_UNIT)
            return snapped
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            # update auxiliary connectors on move for smooth visuals
            with silent_operation("update_event_connectors_on_move"):
                for ne in list(getattr(self, 'note_event_connectors', [])):
                    with silent_operation("update_note_event_connector"):
                        ne.update_path()
                for ef in list(getattr(self, 'event_focus_connectors', [])):
                    with silent_operation("update_event_focus_connector_on_move"):
                        ef.update_path()
                for ee in list(getattr(self, 'event_event_connectors', [])):
                    with silent_operation("update_event_event_connector"):
                        ee.update_path()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # Notify scene that geometry is changing for proper invalidation
            with silent_operation("event_node_selection_changed"):
                self.prepareGeometryChange()
                # Invalidate the area including the selection halo (6px border)
                if self.scene():
                    rect = self.boundingRect().adjusted(-10, -10, 10, 10)
                    self.scene().invalidate(self.mapRectToScene(rect))
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event):
        # Left double-click -> edit event
        with silent_operation("event_node_double_click"):
            if event.button() == Qt.MouseButton.LeftButton and hasattr(self, 'editor'):
                with silent_operation("edit_event_on_double_click"):
                    self.editor.edit_event(self.event)

    def mousePressEvent(self, event):
        with silent_operation("event_node_mouse_press"):
            if event.button() == Qt.MouseButton.LeftButton and hasattr(self, 'editor') and hasattr(self.editor, 'canvas'):
                setattr(self.editor.canvas, '_suspend_layout', True)
                self._move_start_scene_pos = QPointF(self.scenePos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        with silent_operation("event_node_mouse_release"):
            if hasattr(self, 'editor') and hasattr(self.editor, 'canvas'):
                def _resume():
                    with silent_operation("resume_layout_after_event_move"):
                        setattr(self.editor.canvas, '_suspend_layout', False)
                        self.editor.canvas.schedule_frame_update()
                QTimer.singleShot(120, _resume)

    def contextMenuEvent(self, event):
        try:
            menu = QMenu()
            # Only add actions when their preconditions are met
            edit_action = None
            delete_action = None
            link_note_action = None
            unlink_links_action = None
            unlink_child_action = None
            unlink_parent_action = None

            try:
                # Edit event if editor supports editing
                if hasattr(self, 'editor') and getattr(self.editor, 'edit_event', None):
                    edit_action = QAction("Edit Event", menu)
                    menu.addAction(edit_action)
            except Exception:
                edit_action = None

            # Link/unlink note actions only if the canvas exists
            try:
                canvas = getattr(self, 'editor', None) and getattr(self.editor, 'canvas', None)
            except Exception:
                canvas = None
            try:
                if canvas is not None:
                    # link to selected note (only useful when notes present)
                    has_notes = bool(getattr(canvas, '_notes_items', []))
                    if has_notes:
                        link_note_action = QAction("Link Selected Note → This Event", menu)
                        menu.addAction(link_note_action)
                    # unlink existing note links if any are present
                    has_note_links = bool(getattr(self, 'event_focus_connectors', []) or getattr(canvas, '_note_event_connectors', []))
                    if has_note_links:
                        unlink_links_action = QAction("Unlink Notes from This Event", menu)
                        menu.addAction(unlink_links_action)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                # separator before structural link actions if any were added above
                if any((link_note_action, unlink_links_action)):
                    menu.addSeparator()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Unlink child/parent only if scene exposes remove_links_for or there are explicit connectors
            try:
                sc = self.scene()
            except Exception:
                sc = None
            try:
                if sc is not None and hasattr(sc, 'remove_links_for'):
                    unlink_child_action = QAction("Unlink Child", menu)
                    unlink_parent_action = QAction("Unlink Parent", menu)
                    menu.addAction(unlink_child_action)
                    menu.addAction(unlink_parent_action)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                # separator before delete if preceding items exist
                if any((edit_action, link_note_action, unlink_links_action, unlink_child_action, unlink_parent_action)):
                    menu.addSeparator()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            try:
                # Delete event if editor supports deletion
                if hasattr(self, 'editor') and getattr(self.editor, 'delete_event_node', None):
                    delete_action = QAction("Delete Event", menu)
                    menu.addAction(delete_action)
            except Exception:
                delete_action = None

            try:
                from PyQt6.QtGui import QCursor
                act = menu.exec(QCursor.pos())
            except Exception:
                act = menu.exec(event.screenPos())
            if act is edit_action:
                try:
                    self.editor.edit_event(self.event)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            elif act is delete_action:
                try:
                    self.editor.delete_event_node(self)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            elif act is link_note_action:
                try:
                    items = list(self.editor.canvas.selectedItems())
                    note = next((it for it in items if isinstance(it, NoteNode)), None)
                    if not note:
                        QMessageBox.information(self.editor, "Notes", "Select a Note to link to this Event.")
                    else:
                        self.editor.canvas.add_note_event_link(note, self)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            elif act is unlink_links_action:
                try:
                    self.editor.canvas.remove_note_event_links_for(self)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            elif act is unlink_child_action or act is unlink_parent_action:
                try:
                    sc = self.scene()
                    if sc is not None and hasattr(sc, 'remove_links_for'):
                        sc.remove_links_for(self)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

# endregion

# region Note Node

class NoteNode(QGraphicsObject): # Note item (floating, resizable)
    """Resizable sticky note with title, color, close button, and right-click menu.

    Project-scoped attributes: font sizes/colors and persistent id for connections.
    """
    def __init__(self, text: str = "Note", pos: Optional[QPointF] = None, parent: Optional[QObject] = None, title: Optional[str] = None, color: Optional[QColor] = None,
                 title_size: Optional[int] = None, body_size: Optional[int] = None,
                 title_color: Optional[QColor] = None, text_color: Optional[QColor] = None,
                 note_id: Optional[str] = None):
        super().__init__(parent)
        # Base node initialization (flags, hover timer, move tracking)
        with silent_operation("init_note_node_base"):
            with silent_operation("init_note_node_mixin"):
                NodeBase.init_node(self, None, movable=True, selectable=True, accept_drops=False)
            with silent_operation("init_note_node_flags_fallback"):
                self.setFlags(
                    QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                    QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                    QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
                )
        self.setZValue(50)
        # subtle drop shadow for improved styling
        with silent_operation("set_note_drop_shadow"):
            effect = QGraphicsDropShadowEffect()
            effect.setBlurRadius(16)
            effect.setColor(QColor(0, 0, 0, 140))
            effect.setOffset(0, 3)
            self.setGraphicsEffect(effect)
        # stable id for saving/loading and connections
        self.note_id = str(uuid.uuid4())
        with silent_operation("set_note_id"):
            self.note_id = str(note_id) if note_id else str(uuid.uuid4())
        # embedded text
        self._title_item = QGraphicsTextItem(title or "Note", self)
        self._text_item = QGraphicsTextItem(text, self)
        # Cache note and its text items to reduce paint cost when zooming/panning
        with silent_operation("set_note_cache_mode"):
            from PyQt6.QtWidgets import QGraphicsItem as _QI
            self.setCacheMode(_QI.CacheMode.DeviceCoordinateCache)
            with silent_operation("set_text_items_cache_mode"):
                self._title_item.setCacheMode(_QI.CacheMode.DeviceCoordinateCache)
                self._text_item.setCacheMode(_QI.CacheMode.DeviceCoordinateCache)
        # font colors, sizes (can be overridden by project defaults)
        self._title_color = QColor(title_color) if title_color is not None else QColor(20, 20, 20)
        self._text_color = QColor(text_color) if text_color is not None else QColor(30, 30, 30)
        self._title_item.setDefaultTextColor(self._title_color)
        self._text_item.setDefaultTextColor(self._text_color)
        tf = self._title_item.font(); tf.setPointSize(int(title_size) if title_size else 11); tf.setBold(True); self._title_item.setFont(tf)
        f = self._text_item.font(); f.setPointSize(int(body_size) if body_size else 11); self._text_item.setFont(f)
        self._text_item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self._title_item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        # geometry
        self._padding = 10
        self._rect = QRectF(0, 0, 260, 120)
        self._handle_size = 12
        self._resizing = False
        # color state
        self._bg_color = QColor(255, 255, 210, 220) if color is None else QColor(color)
        # track connections this note participates in (NoteConnectionLine instances)
        self._note_connections = []  # type: list
        # track note→focus connectors this note participates in
        self._note_focus_connectors = []  # type: list
        # track note→event connectors this note participates in
        self._note_event_connectors = []  # type: list
        # transient state for undoable move/resize
        self._move_start_scene_pos = None
        self._resize_start_rect = None
        if pos is not None:
            self.setPos(pos)
        self._layout_text()
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)

    # Quick context menu for common actions
    def contextMenuEvent(self, event: 'QGraphicsSceneContextMenuEvent') -> None:
        try:
            menu = QMenu()
            copy_title = menu.addAction("Copy Title")
            copy_body = menu.addAction("Copy Body")
            menu.addSeparator()
            inc_font = menu.addAction("Increase Body Font")
            dec_font = menu.addAction("Decrease Body Font")
            menu.addSeparator()
            clr_yellow = menu.addAction("Yellow Note")
            clr_blue = menu.addAction("Blue Note")
            clr_green = menu.addAction("Green Note")
            color_act = menu.addAction("Pick Color…")
            menu.addSeparator()
            title_act = menu.addAction("Rename Title…")
            del_act = menu.addAction("Delete Note")
            unlink_child_action = menu.addAction("Unlink Child")
            unlink_parent_action = menu.addAction("Unlink Parent")
            try:
                from PyQt6.QtGui import QCursor
                act = menu.exec(QCursor.pos())
            except Exception:
                act = menu.exec(event.screenPos())
            
            if not act:
                return

            if act is unlink_child_action or act is unlink_parent_action:
                try:
                    sc = self.scene()
                    if sc is not None and hasattr(sc, 'remove_links_for'):
                        sc.remove_links_for(self)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            elif act is copy_title:
                QApplication.clipboard().setText(self._title_item.toPlainText())
            elif act is copy_body:
                QApplication.clipboard().setText(self._text_item.toPlainText())
            elif act is inc_font or act is dec_font:
                f = self._text_item.font()
                delta = 1 if act is inc_font else -1
                f.setPointSize(max(6, min(48, f.pointSize() + delta)))
                self._text_item.setFont(f)
                self._layout_text()
            elif act in (clr_yellow, clr_blue, clr_green):
                if act is clr_yellow:
                    self._bg_color = QColor(255, 255, 210, 220)
                elif act is clr_blue:
                    self._bg_color = QColor(210, 230, 255, 220)
                elif act is clr_green:
                    self._bg_color = QColor(210, 255, 230, 220)
                self.update()
            elif act == color_act:
                col = QColorDialog.getColor(self._bg_color, None, "Pick Note Color")
                if col.isValid():
                    self._bg_color = QColor(col)
                    self.update()
            elif act == title_act:
                text, ok = QInputDialog.getText(None, "Rename Note", "Title:", text=self._title_item.toPlainText())
                if ok:
                    self._title_item.setPlainText(text)
                    self._layout_text()
                    self.update()
            elif act == del_act:
                self._delete_self()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _layout_text(self):
        # Title sits in header area, body text below
        header_h = 22
        inner = self._rect.adjusted(self._padding, self._padding + header_h, -self._padding, -self._padding)
        # Title width uses full inner width
        self._title_item.setTextWidth(max(10.0, self._rect.width() - 2 * self._padding))
        self._title_item.setPos(self._rect.left() + self._padding, self._rect.top() + 2)
        self._text_item.setTextWidth(max(10.0, inner.width()))
        self._text_item.setPos(inner.topLeft())
        # Grow height if needed to fit text
        doc_h = self._text_item.document().size().height()
        desired_inner_h = max(inner.height(), doc_h)
        total_h = desired_inner_h + 2 * self._padding
        if total_h > self._rect.height():
            self.prepareGeometryChange()
            self._rect.setHeight(total_h)

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-1, -1, 1, 1)

    def paint(self, painter: QPainter, option, widget=None):
        # body
        bg = QColor(self._bg_color)
        pen = QColor(180, 170, 80)
        halo = None
        if self.isSelected():
            pen = QColor(80, 120, 200)
            halo = QColor(80, 120, 200, 70)
        painter.setBrush(QBrush(bg))
        # draw halo behind note if selected
        with silent_operation("draw_note_selection_halo"):
            if halo is not None:
                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(halo))
                painter.drawRoundedRect(self._rect.adjusted(-4, -4, 4, 4), 8, 8)
                painter.restore()
        pen_w = 3 if self.isSelected() else 1
        painter.setPen(QPen(pen, pen_w))
        painter.drawRoundedRect(self._rect, 6, 6)
        # header bar with close button area
        header = QRectF(self._rect.left(), self._rect.top(), self._rect.width(), 22)
        painter.fillRect(header, QColor(255, 245, 150, 230))
        # close button (small X at right)
        cb_size = 14
        self._close_rect = QRectF(self._rect.right() - cb_size - 4, self._rect.top() + 4, cb_size, cb_size)
        painter.setBrush(QBrush(QColor(220, 70, 70)))
        painter.setPen(QPen(QColor(200, 50, 50)))
        painter.drawRect(self._close_rect)
        painter.setPen(QPen(Qt.GlobalColor.white, 2))
        painter.drawLine(self._close_rect.topLeft() + QPointF(3, 3), self._close_rect.bottomRight() - QPointF(3, 3))
        painter.drawLine(self._close_rect.topRight() + QPointF(-3, 3), self._close_rect.bottomLeft() + QPointF(3, -3))
        # resize grip
        hs = self._handle_size
        grip = QRectF(self._rect.right() - hs, self._rect.bottom() - hs, hs, hs)
        painter.fillRect(grip, QColor(120, 120, 120))

    def _handle_hit(self, pos: QPointF) -> bool:
        hs = self._handle_size
        return QRectF(self._rect.right() - hs, self._rect.bottom() - hs, hs, hs).contains(pos)

    def mousePressEvent(self, event: 'QGraphicsSceneMouseEvent'):
        if event.button() == Qt.MouseButton.LeftButton and self._handle_hit(event.pos()):
            self._resizing = True
            event.accept()
            return
        # quick close
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, '_close_rect') and self._close_rect.contains(event.pos()):
            self._delete_self()
            event.accept()
            return
        # capture starting state for undo commands
        self._move_start_scene_pos = None
        self._resize_start_rect = None
        with silent_operation("capture_note_move_start"):
            self._move_start_scene_pos = self.scenePos()
            self._resize_start_rect = QRectF(self._rect)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: 'QGraphicsSceneMouseEvent'):
        if self._resizing:
            p = event.pos()
            min_w, min_h = 120.0, 60.0
            new_w = max(min_w, p.x())
            new_h = max(min_h, p.y())
            self.prepareGeometryChange()
            self._rect.setWidth(new_w)
            self._rect.setHeight(new_h)
            self._layout_text()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: 'QGraphicsSceneMouseEvent'):
        self._resizing = False
        super().mouseReleaseEvent(event)
        # If an undo stack is available, push commands for move/resize
        with silent_operation("push_note_move_resize_undo"):
            sc = self.scene()
            main = getattr(sc, 'parent', None) if sc is not None else None
            uw = getattr(main, 'undo_stack', None)
            if uw is not None:
                end_pos = self.scenePos()
                end_rect = QRectF(self._rect)
                if self._move_start_scene_pos is not None and end_pos != self._move_start_scene_pos:
                    uw.push(MoveNoteCommand(self, self._move_start_scene_pos, end_pos))
                if self._resize_start_rect is not None and end_rect != self._resize_start_rect:
                    uw.push(ResizeNoteCommand(self, self._resize_start_rect, end_rect))


    def _delete_self(self):
        sc = self.scene()
        with silent_operation("push_delete_note_undo"):
            main = getattr(sc, 'parent', None) if sc is not None else None
            uw = getattr(main, 'undo_stack', None)
            if uw is not None:
                cmd = DeleteNoteCommand(main, self)
                uw.push(cmd)
                return
        if sc is not None:
            with silent_operation("remove_note_from_scene"):
                if hasattr(sc, 'safe_remove_item'):
                    sc.safe_remove_item(self)
                else:
                    with silent_operation("remove_note_item"):
                        sc.removeItem(self)
            with silent_operation("remove_from_notes_items"):
                if hasattr(sc, '_notes_items') and self in getattr(sc, '_notes_items'):
                    sc._notes_items.remove(self)
            # remove any note connections referencing this note
            with silent_operation("remove_note_connections"):
                if hasattr(sc, 'remove_note_connections_for'):
                    sc.remove_note_connections_for(self)
            # remove any note→focus links referencing this note
            with silent_operation("remove_note_focus_links"):
                if hasattr(sc, 'remove_note_focus_links_for'):
                    sc.remove_note_focus_links_for(self)
        self.deleteLater()

    def to_dict(self) -> Dict[str, Any]:
        tfont = QFont(); bfont = QFont()
        with silent_operation("get_note_fonts"):
            tfont = self._title_item.font(); bfont = self._text_item.font()
        return {
            'id': self.note_id,
            'title': self._title_item.toPlainText(),
            'text': self._text_item.toPlainText(),
            'x': float(self.scenePos().x()),
            'y': float(self.scenePos().y()),
            'w': float(self._rect.width()),
            'h': float(self._rect.height()),
            'color': self._bg_color.name(QColor.NameFormat.HexArgb),
            'title_size': int(tfont.pointSize() or 11),
            'body_size': int(bfont.pointSize() or 11),
            'title_color': QColor(self._title_item.defaultTextColor()).name(QColor.NameFormat.HexArgb),
            'text_color': QColor(self._text_item.defaultTextColor()).name(QColor.NameFormat.HexArgb),
        }

    def set_visible(self, visible: bool) -> None:
        self.setVisible(visible)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'NoteNode':
        pos = QPointF(float(d.get('x', 0.0)), float(d.get('y', 0.0)))
        title = d.get('title') or 'Note'
        color = QColor(d.get('color')) if d.get('color') else None
        it = NoteNode(
            str(d.get('text', 'Note')), pos,
            title=title, color=color,
            title_size=d.get('title_size'), body_size=d.get('body_size'),
            title_color=QColor(d.get('title_color')) if d.get('title_color') else None,
            text_color=QColor(d.get('text_color')) if d.get('text_color') else None,
            note_id=d.get('id')
        )
        w = float(d.get('w', 260.0)); h = float(d.get('h', 120.0))
        it._rect = QRectF(0, 0, max(60.0, w), max(40.0, h))
        it._layout_text()
        return it

    # connection registration for quick updates on move
    def _register_note_connection(self, line: 'NoteConnectionLine') -> None:
        with silent_operation("register_note_connection"):
            if line not in self._note_connections:
                self._note_connections.append(line)

    def _unregister_note_connection(self, line: 'NoteConnectionLine') -> None:
        with silent_operation("unregister_note_connection"):
            if line in self._note_connections:
                self._note_connections.remove(line)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            with silent_operation("update_note_connections_on_move"):
                for ln in list(self._note_connections):
                    with silent_operation("update_note_line_path"):
                        ln.update_path()
                # also update note→focus connectors
                for nf in list(self._note_focus_connectors):
                    with silent_operation("update_note_focus_connector"):
                        nf.update_path()
                # also update note→event connectors
                for ne in list(getattr(self, '_note_event_connectors', [])):
                    with silent_operation("update_note_event_connector"):
                        ne.update_path()
        return super().itemChange(change, value)

    # helper for applying defaults from project settings
    def apply_defaults(self, defaults: Dict[str, Any]) -> None:
        with silent_operation("apply_note_defaults"):
            if not defaults:
                return
            # colors
            if defaults.get('title_color'):
                self._title_color = QColor(defaults['title_color'])
                self._title_item.setDefaultTextColor(self._title_color)
            if defaults.get('text_color'):
                self._text_color = QColor(defaults['text_color'])
                self._text_item.setDefaultTextColor(self._text_color)
            if defaults.get('bg_color'):
                self._bg_color = QColor(defaults['bg_color'])
            # font sizes
            tf = self._title_item.font();
            if defaults.get('title_size'):
                tf.setPointSize(int(defaults['title_size']))
            tf.setBold(True); self._title_item.setFont(tf)
            bf = self._text_item.font();
            if defaults.get('body_size'):
                bf.setPointSize(int(defaults['body_size']))
            self._text_item.setFont(bf)
            self.update()

# endregion

# region Graphics View & Note/Event Connectors

class EnhancedGraphicsView(QGraphicsView): # Graphics view with pan/zoom
    """Enhanced graphics view with panning and zooming"""
    def __init__(self, scene):
        super().__init__(scene)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        # View optimization flags to reduce per-frame overhead
        with silent_operation("set_view_optimization_flags"):
            self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, True)
            self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
            self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontClipPainter, True)
        # Cache static background to avoid redrawing empty areas
        with silent_operation("set_view_cache_mode"):
            self.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)
        # Use SmartViewportUpdate by default to reduce full-scene repaints while
        # keeping visuals correct. Falls back to Minimal if Smart isn't available.
        with silent_operation("set_viewport_update_mode"):
            self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        with silent_operation("set_viewport_update_mode_fallback"):
            self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Panning state
        self.panning = False
        self.pan_start_x = 0
        self.pan_start_y = 0
        # Zoom limits (reasonable for an HOI4 world map)
        # min 5% (0.05) prevents extreme zoom-out that makes the map tiny
        # max 800% (8.0) provides enough zoom for details without going absurd
        self.MIN_SCALE = 0.05
        self.MAX_SCALE = 8.0

        # Accept focus to receive key events (Escape to cancel connection mode)
        with silent_operation("set_focus_policy"):
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _update_antialiasing(self):
        """Adapt antialiasing based on zoom for smoother interaction."""
        try:
            s = self.transform().m11()
        except Exception:
            s = 1.0
        try:
            # Disable AA when zoomed far out for speed; enable when zoomed in
            self.setRenderHint(QPainter.RenderHint.Antialiasing, bool(s >= 0.6))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def wheelEvent(self, event):
        """Zoom exactly about the mouse position (no drift)."""
        try:
            # Determine zoom direction and factor
            delta = 0
            try:
                delta = event.angleDelta().y()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            if delta == 0:
                try:
                    delta = event.pixelDelta().y()
                except Exception:
                    delta = 0
            if delta == 0:
                return super().wheelEvent(event)
            zoom_in = delta > 0
            factor = 1.15 if zoom_in else (1.0 / 1.15)

            # Clamp zoom to configured limits
            current_scale = self.transform().m11()
            new_scale = current_scale * factor
            if new_scale > self.MAX_SCALE:
                # scale only up to max
                factor = self.MAX_SCALE / current_scale
                new_scale = self.MAX_SCALE
            if new_scale < self.MIN_SCALE:
                factor = self.MIN_SCALE / current_scale
                new_scale = self.MIN_SCALE
            # If factor would be 1.0, no-op
            if abs(factor - 1.0) < 1e-9:
                return

            # Scale about mouse using AnchorUnderMouse
            self.scale(factor, factor)
            # Adjust render hints to current zoom level
            with silent_operation("update_antialiasing_on_zoom"):
                self._update_antialiasing()

            # Immediately check if zoom crossed the dynamic scaling threshold
            with silent_operation("check_scaling_cache_on_zoom"):
                sc = self.scene()
                if sc is not None and hasattr(sc, '_check_and_invalidate_scaling_cache'):
                    sc._check_and_invalidate_scaling_cache()

            # Refresh status (zoom label) on the owning window/editor if possible
            with silent_operation("update_status_on_zoom"):
                # try parent chain first
                p = self.parent()
                if p is not None and hasattr(p, 'update_status'):
                    p.update_status()
                else:
                    w = self.window()
                    if w is not None and hasattr(w, 'update_status'):
                        w.update_status()

            event.accept()
            # schedule cull after zoom so we hide/show items appropriately
            with silent_operation("schedule_cull_after_zoom"):
                sc = self.scene()
                if sc is not None and hasattr(sc, 'schedule_cull'):
                    sc.schedule_cull()
            # update zoom overlay if present
            with silent_operation("update_zoom_overlay"):
                if getattr(self, '_zoom_overlay', None) is not None:
                    self._zoom_overlay.setText(f"{int(new_scale*100)}%")
                    self._zoom_overlay.adjustSize()
                    # reposition
                    with silent_operation("reposition_zoom_overlay"):
                        w = self.width(); h = self.height(); lw = self._zoom_overlay.width(); lh = self._zoom_overlay.height()
                        self._zoom_overlay.move(max(6, w - lw - 8), max(6, h - lh - 8))
        except Exception:
            return super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            # Start panning
            self.panning = True
            self.pan_start_x = event.position().x()
            self.pan_start_y = event.position().y()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            # Temporarily disable AA during panning for smoother movement
            with silent_operation("disable_aa_for_panning"):
                self.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        elif event.button() == Qt.MouseButton.LeftButton:
            # If clicking on a movable item, disable view rubber-band so the item stays attached to mouse
            with silent_operation("check_item_for_drag_mode"):
                viewport_pt = event.position().toPoint()
                item = self.itemAt(viewport_pt)
                if item is not None and (item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable):
                    self.setDragMode(QGraphicsView.DragMode.NoDrag)
                else:
                    self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            super().mousePressEvent(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.panning:
            # Pan the view
            delta_x = event.position().x() - self.pan_start_x
            delta_y = event.position().y() - self.pan_start_y
            # Translate the view
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta_x)
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta_y)
            )
            self.pan_start_x = event.position().x()
            self.pan_start_y = event.position().y()
            # schedule cull for panning during user drag (throttled)
            with silent_operation("schedule_cull_during_pan"):
                sc = self.scene()
                if sc is not None and hasattr(sc, 'schedule_cull'):
                    sc.schedule_cull(80)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            # End panning
            self.panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            # Force full scene and viewport update to clear any artifacts
            with silent_operation("invalidate_scene_after_pan"):
                sc = self.scene()
                if sc is not None:
                    sc.invalidate()
                self.viewport().update()
            # Restore antialiasing according to current zoom
            with silent_operation("restore_aa_after_pan"):
                self._update_antialiasing()
        else:
            super().mouseReleaseEvent(event)

        # Always restore default rubber-band drag mode on release (unless panning)
        try:
            if not self.panning:
                self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # schedule a cull once after mouse release (user stopped interacting)
        try:
            sc = self.scene()
            if sc is not None and hasattr(sc, 'schedule_cull'):
                sc.schedule_cull(120)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def resizeEvent(self, event):
        # keep zoom overlay positioned bottom-right
        try:
            if getattr(self, '_zoom_overlay', None) is not None:
                w = self.width(); h = self.height(); lw = self._zoom_overlay.width(); lh = self._zoom_overlay.height()
                self._zoom_overlay.move(max(6, w - lw - 8), max(6, h - lh - 8))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return super().resizeEvent(event)

    def setTransform(self, transform, combine=False):
        """Override setTransform to sanitize negative scale components.

        This prevents accidental horizontal/vertical flipping caused by a
        negative scale on either axis. We keep shear/rotation intact and
        only force the X/Y scale components to be non-negative.
        """
        try:
            # Import locally to avoid touching module-level imports
            from PyQt6.QtGui import QTransform
            # Extract matrix elements
            m11 = transform.m11(); m12 = transform.m12(); m13 = transform.m13()
            m21 = transform.m21(); m22 = transform.m22(); m23 = transform.m23()
            m31 = transform.m31(); m32 = transform.m32(); m33 = transform.m33()
            # If either scale component is negative, replace with absolute value
            if m11 < 0 or m22 < 0:
                t = QTransform(abs(m11), m12, m13, m21, abs(m22), m23, m31, m32, m33)
                return super().setTransform(t, combine)
        except Exception:
            # If anything goes wrong, fall back to default behavior
            pass
        return super().setTransform(transform, combine)

    def keyPressEvent(self, event):
        try:
            if event.key() == Qt.Key.Key_Escape:
                # Cancel any connection mode active on the scene
                try:
                    scene = self.scene()
                    if hasattr(scene, 'cancel_connection_mode'):
                        scene.cancel_connection_mode()
                        event.accept()
                        return
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Backwards-compatibility: older code/tests reference NoteItem — keep an alias
        NoteItem = NoteNode
        try:
            super().keyPressEvent(event)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Handle Delete / Backspace centrally to avoid ambiguous shortcuts
        try:
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                # Try to route to owning main window to perform unified deletion
                try:
                    mw = None
                    # Prefer the scene's editor reference if available
                    sc = self.scene()
                    if sc is not None:
                        mw = getattr(sc, 'editor', None)
                    # Fallback to view parent()
                    if mw is None and hasattr(self, 'parent') and callable(getattr(self, 'parent')):
                        try:
                            mw = self.parent()
                        except Exception:
                            mw = None
                    if mw is not None and hasattr(mw, 'delete_selected_items'):
                        # If Backspace was pressed, perform a non-confirming deletion
                        try:
                            if event.key() == Qt.Key.Key_Backspace:
                                mw.delete_selected_items(confirm=False)
                            else:
                                mw.delete_selected_items()
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        event.accept()
                        return
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

# Module-level compatibility alias for older code/tests that import `NoteItem`.
# Placed at module scope so `from _focusGUI import NoteItem` works reliably.
try:
    NoteItem = NoteNode
except Exception:
    # If NoteNode isn't defined yet for some reason during import, ignore.
    pass

class NoteConnectionLine(ConnectionItem):
    """Curved, adjustable connection line between two NoteNode objects.

    Supports per-canvas curve strength multiplier, optional midpoint label, arrowhead,
    and per-connection manual overrides (angle/offset/label) persisted in project JSON.
    """
    def __init__(self, a: NoteNode, b: NoteNode, color: Optional[QColor] = None, width: int = 2):
        super().__init__(color=color, width=width, z=-0.25)
        self.a = a
        self.b = b
        # manual overrides for this connection (persisted when saving project)
        # angle: float degrees to rotate computed perpendicular offset (None=auto)
        # offset: float additional perpendicular offset in pixels (signed)
        # label: optional string to draw at midpoint
        self.manual_angle = None
        self.manual_offset = 0.0
        self.label = ''
        # allow right-click context menu and selection for per-connection edits
        try:
            self.setAcceptedMouseButtons(Qt.MouseButton.RightButton)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # recalc path now that endpoints are set
        self.update_path()

    def paint(self, painter: QPainter, option, widget=None):
        # Draw path + arrow (handled in update_path path) then draw optional label at midpoint
        try:
            super().paint(painter, option, widget)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            lab = getattr(self, 'label', None)
            if lab:
                mp = getattr(self, '_midpoint', None)
                if mp is None:
                    return
                painter.save()
                try:
                    f = painter.font()
                    f.setPointSize(max(8, f.pointSize() - 2))
                    painter.setFont(f)
                    fm = painter.fontMetrics()
                    text = str(lab)
                    w = fm.horizontalAdvance(text) + 8
                    h = fm.height() + 4
                    rect = QRectF(mp.x() - w/2.0, mp.y() - h/2.0, w, h)
                    # background box
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
                    painter.drawRoundedRect(rect, 4, 4)
                    # text
                    painter.setPen(QPen(QColor(40, 40, 40)))
                    painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine), text)
                finally:
                    painter.restore()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def contextMenuEvent(self, event):
        try:
            menu = QMenu()
            edit_label = None
            edit_offset = None
            remove = None
            unlink_child_action = None
            unlink_parent_action = None

            try:
                # Provide edit label/offset when the connection supports it
                edit_label = QAction('Edit Label...', menu)
                menu.addAction(edit_label)
                edit_offset = QAction('Set Offset/Angle...', menu)
                menu.addAction(edit_offset)
            except Exception:
                edit_label = None; edit_offset = None

            try:
                # Offer unlink actions only if scene supports link removal
                sc = self.scene()
            except Exception:
                sc = None
            try:
                if sc is not None and hasattr(sc, 'remove_connection'):
                    remove = QAction('Remove Connection', menu)
                    menu.addAction(remove)
                if sc is not None and hasattr(sc, 'remove_links_for'):
                    unlink_child_action = QAction('Unlink Child', menu)
                    unlink_parent_action = QAction('Unlink Parent', menu)
                    menu.addAction(unlink_child_action)
                    menu.addAction(unlink_parent_action)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            try:
                from PyQt6.QtGui import QCursor
                act = menu.exec(QCursor.pos())
            except Exception:
                act = menu.exec(event.screenPos())
            if act is unlink_child_action or act is unlink_parent_action:
                try:
                    sc = self.scene()
                    if sc is not None and hasattr(sc, 'remove_connection'):
                        # Prefer undoable removal if main window exposes an undo stack
                        try:
                            main = getattr(sc, 'parent', None) or getattr(sc, 'parent', None)
                        except Exception:
                            main = None
                        uw = getattr(main, 'undo_stack', None) if main is not None else None
                        try:
                            if uw is not None:
                                # Use the RemoveConnectionCommand so the action is undoable
                                uw.push(RemoveConnectionCommand(sc, self))
                            else:
                                sc.remove_connection(self)
                        except Exception:
                            try:
                                sc.remove_connection(self)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            if act == edit_label:
                text, ok = QInputDialog.getText(None, 'Connection Label', 'Text:', text=self.label or '')
                if ok:
                    self.label = str(text)
                    self.update_path()
            elif act == edit_offset:
                # simple two-field dialog: offset (float) and angle (degrees or blank)
                dlg = QDialog()
                dlg.setWindowTitle('Offset / Angle')
                layout = QFormLayout(dlg)
                off_edit = QLineEdit(str(self.manual_offset))
                ang_edit = QLineEdit('' if self.manual_angle is None else str(self.manual_angle))
                layout.addRow('Offset (px):', off_edit)
                layout.addRow('Angle (deg, blank=auto):', ang_edit)
                btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                layout.addWidget(btns)
                btns.accepted.connect(dlg.accept); btns.rejected.connect(dlg.reject)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    try:
                        self.manual_offset = float(off_edit.text())
                    except Exception:
                        self.manual_offset = 0.0
                    try:
                        atext = ang_edit.text().strip()
                        self.manual_angle = None if atext == '' else float(atext)
                    except Exception:
                        self.manual_angle = None
                    self.update_path()
            elif act == remove:
                sc = self.scene()
                try:
                    if sc is not None:
                        # Prefer undoable removal via RemoveConnectionCommand
                        try:
                            main = getattr(sc, 'parent', None) or getattr(sc, 'parent', None)
                        except Exception:
                            main = None
                        uw = getattr(main, 'undo_stack', None) if main is not None else None
                        try:
                            if uw is not None:
                                uw.push(RemoveConnectionCommand(sc, self))
                            else:
                                if hasattr(sc, 'safe_remove_item'):
                                    sc.safe_remove_item(self)
                                else:
                                    try:
                                        sc.removeItem(self)
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        except Exception:
                            try:
                                if hasattr(sc, 'safe_remove_item'):
                                    sc.safe_remove_item(self)
                                else:
                                    try:
                                        sc.removeItem(self)
                                    except Exception as e:
                                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def update_path(self) -> None:
        try:
            # anchor to the perimeter of each note's bounding rect so the
            # connector visually attaches to the edge instead of the internal
            # origin; also compute a smooth cubic curve that recenters itself
            # based on the relative positions of the notes.
            import math

            ra = self.a.sceneBoundingRect()
            rb = self.b.sceneBoundingRect()
            ca = ra.center()
            cb = rb.center()

            def perimeter_anchor(rect: QRectF, target: QPointF) -> QPointF:
                # Ray from center to target; find intersection with rect edges
                c = rect.center()
                dx = target.x() - c.x()
                dy = target.y() - c.y()
                hw = max(1.0, rect.width() / 2.0)
                hh = max(1.0, rect.height() / 2.0)
                if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                    return QPointF(c.x(), c.y())
                sx = float('inf') if abs(dx) < 1e-6 else (hw / abs(dx))
                sy = float('inf') if abs(dy) < 1e-6 else (hh / abs(dy))
                s = min(sx, sy)
                # scale slightly inward to avoid overlapping the border
                s = max(0.0, min(1.0, s * 0.98))
                return QPointF(c.x() + dx * s, c.y() + dy * s)

            a_pt = perimeter_anchor(ra, cb)
            b_pt = perimeter_anchor(rb, ca)

            # Early-out: skip rebuild if endpoints/LOD unchanged (compute above in caller)
            # LOD: simplified straight-line rendering when zoomed out
            try:
                if self._use_simple_lod():
                    path = QPainterPath(a_pt)
                    path.lineTo(b_pt)
                    self.setPath(path)
                    # store midpoint for potential label
                    try:
                        self._midpoint = QPointF((a_pt.x() + b_pt.x()) * 0.5, (a_pt.y() + b_pt.y()) * 0.5)
                    except Exception:
                        self._midpoint = None
                    return
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # If both anchors are extremely close, draw a tiny loop to remain visible
            vx = b_pt.x() - a_pt.x()
            vy = b_pt.y() - a_pt.y()
            dist = math.hypot(vx, vy)
            if dist < 1.0:
                path = QPainterPath(a_pt)
                path.lineTo(a_pt + QPointF(1.0, 0.0))
                self.setPath(path)
                return

            # Compute perpendicular for curvature
            nx = -vy / dist
            ny = vx / dist

            # curvature magnitude scales with distance but is clamped, and modulated by canvas setting
            try:
                canvas = getattr(getattr(self.a, 'scene', lambda: None)(), 'parent', None) or getattr(getattr(self.a, 'editor', None), 'canvas', None)
                multiplier = float(getattr(canvas, 'note_connection_curve_strength', 1.0))
            except Exception:
                multiplier = 1.0
            curv = max(6.0, min(dist * 0.25 * multiplier, 400.0))

            # Use control points at 1/3 and 2/3 along the straight line and offset by perp
            p1 = QPointF(a_pt.x() + vx * 0.33, a_pt.y() + vy * 0.33)
            p2 = QPointF(a_pt.x() + vx * 0.66, a_pt.y() + vy * 0.66)
            # Choose sign to bow the curve outward relative to canvas center to reduce overlap
            sign = 1.0 if (vx * ny - vy * nx) >= 0 else -1.0
            # apply manual angle/offset overrides if present
            mo = getattr(self, 'manual_offset', 0.0) or 0.0
            ma = getattr(self, 'manual_angle', None)
            if ma is not None:
                # rotate the perpendicular by manual_angle degrees
                ang = math.radians(float(ma))
                rnx = math.cos(ang) * nx - math.sin(ang) * ny
                rny = math.sin(ang) * nx + math.cos(ang) * ny
                nx, ny = rnx, rny
            ctrl1 = QPointF(p1.x() + nx * (curv + mo) * sign, p1.y() + ny * (curv + mo) * sign)
            ctrl2 = QPointF(p2.x() + nx * (curv + mo) * sign, p2.y() + ny * (curv + mo) * sign)

            path = QPainterPath(a_pt)
            path.cubicTo(ctrl1, ctrl2, b_pt)
            self.setPath(path)

            # draw a small arrowhead at the midpoint direction
            try:
                mid_t = 0.5
                # compute tangent at t by numerical derivative of cubic Bezier
                def bezier_point(t, p0, p1, p2, p3):
                    return (
                        (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3
                    )
                # convert QPointF to tuple for arithmetic
                p0 = (a_pt.x(), a_pt.y())
                p1t = (ctrl1.x(), ctrl1.y())
                p2t = (ctrl2.x(), ctrl2.y())
                p3 = (b_pt.x(), b_pt.y())
                # point positions
                pt_mid = bezier_point(mid_t, p0, p1t, p2t, p3)
                eps = 1e-4
                pt_prev = bezier_point(mid_t - eps, p0, p1t, p2t, p3)
                pt_next = bezier_point(mid_t + eps, p0, p1t, p2t, p3)
                tx = pt_next[0] - pt_prev[0]
                ty = pt_next[1] - pt_prev[1]
                tang_len = math.hypot(tx, ty) or 1.0
                ux = tx / tang_len; uy = ty / tang_len
                # arrow parameters
                arrow_size = 8.0
                # base center for arrow slightly before midpoint to avoid overlap
                bx = pt_mid[0] - ux * 6.0
                by = pt_mid[1] - uy * 6.0
                # two wing points
                left_x = bx - uy * (arrow_size * 0.6)
                left_y = by + ux * (arrow_size * 0.6)
                right_x = bx + uy * (arrow_size * 0.6)
                right_y = by - ux * (arrow_size * 0.6)
                tip_x = bx + ux * arrow_size
                tip_y = by + uy * arrow_size
                arrow_path = QPainterPath(QPointF(tip_x, tip_y))
                arrow_path.lineTo(QPointF(left_x, left_y))
                arrow_path.lineTo(QPointF(right_x, right_y))
                arrow_path.closeSubpath()
                # create a combined path so arrow uses same pen/brush
                full = QPainterPath(path)
                full.addPath(arrow_path)
                self.setPath(full)
            except Exception:
                # ignore drawing arrow if any math fails
                pass

            # store midpoint label position for painting by scene overlay if needed
            try:
                self._midpoint = QPointF((a_pt.x() + b_pt.x()) * 0.5, (a_pt.y() + b_pt.y()) * 0.5)
            except Exception:
                self._midpoint = None
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class NoteFocusConnector(ConnectionItem): # note > focus connector (annotation link)
    """Dashed connector between a NoteNode and a FocusNode (distinct from lineage)."""
    def __init__(self, note: NoteNode, focus_node: 'FocusNode', color: Optional[QColor] = None, width: int = 2):
        base_col = QColor(60, 140, 200) if color is None else QColor(color)
        super().__init__(base_col, width=width, z=-0.3)
        self.note = note
        self.focus_node = focus_node
        try:
            pen = QPen(base_col, max(1, int(width)), Qt.PenStyle.DashDotLine)
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.setPen(pen)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_path()

    def update_path(self) -> None:
        try:
            na = self.note.sceneBoundingRect().center()
            fb = self.focus_node.sceneBoundingRect().center()
            # Early-out: avoid rebuilding if nothing changed
            try:
                current_lod = self._use_simple_lod()
                current_eps = self._endpoints_state()
                if self._last_lod_mode == current_lod and self._last_endpoints == current_eps:
                    return
                self._last_lod_mode = current_lod
                self._last_endpoints = current_eps
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # LOD: if zoomed out, draw simple straight line
            try:
                if self._use_simple_lod():
                    path = QPainterPath(na)
                    path.lineTo(fb)
                    self.setPath(path)
                    return
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            path = QPainterPath(na)
            mid = (na + fb) * 0.5
            ctrl1 = QPointF(mid.x(), na.y())
            ctrl2 = QPointF(mid.x(), fb.y())
            path.cubicTo(ctrl1, ctrl2, fb)
            self.setPath(path)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class NoteEventConnector(ConnectionItem):
    """Dashed connector between a NoteNode and an EventNode (distinct channel)."""
    def __init__(self, note: NoteNode, event_node: 'EventNode', color: Optional[QColor] = None, width: int = 2):
        base_col = QColor(140, 60, 200) if color is None else QColor(color)
        super().__init__(base_col, width=width, z=-0.3)
        self.note = note
        self.event_node = event_node
        try:
            pen = QPen(base_col, max(1, int(width)), Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.setPen(pen)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_path()

    def update_path(self) -> None:
        try:
            na = self.note.sceneBoundingRect().center()
            eb = self.event_node.sceneBoundingRect().center()
            # Early-out: avoid rebuilding if nothing changed
            try:
                current_lod = self._use_simple_lod()
                current_eps = self._endpoints_state()
                if self._last_lod_mode == current_lod and self._last_endpoints == current_eps:
                    return
                self._last_lod_mode = current_lod
                self._last_endpoints = current_eps
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # LOD: draw straight line when zoomed out
            try:
                if self._use_simple_lod():
                    path = QPainterPath(na)
                    path.lineTo(eb)
                    self.setPath(path)
                    return
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            path = QPainterPath(na)
            mid = (na + eb) * 0.5
            ctrl1 = QPointF(mid.x(), na.y())
            ctrl2 = QPointF(mid.x(), eb.y())
            path.cubicTo(ctrl1, ctrl2, eb)
            self.setPath(path)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def set_color(self, color: QColor) -> None:
        try:
            self.color = QColor(color)
            p = self.pen(); p.setColor(self.color); self.setPen(p)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class EventFocusConnector(ConnectionItem):
    """Dashed connector between an EventNode and a FocusNode."""
    def __init__(self, event_node: 'EventNode', focus_node: 'FocusNode', color: Optional[QColor] = None, width: int = 2):
        base_col = QColor(70, 160, 120) if color is None else QColor(color)
        super().__init__(base_col, width=width, z=-0.3)
        self.event_node = event_node
        self.focus_node = focus_node
        try:
            pen = QPen(base_col, max(1, int(width)), Qt.PenStyle.DashDotDotLine)
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.setPen(pen)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_path()

    def update_path(self) -> None:
        try:
            ea = self.event_node.sceneBoundingRect().center()
            fb = self.focus_node.sceneBoundingRect().center()
            # Early-out: avoid rebuilding if nothing changed
            try:
                current_lod = self._use_simple_lod()
                current_eps = self._endpoints_state()
                if self._last_lod_mode == current_lod and self._last_endpoints == current_eps:
                    return
                self._last_lod_mode = current_lod
                self._last_endpoints = current_eps
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # LOD: simplified straight line when zoomed out
            try:
                if self._use_simple_lod():
                    path = QPainterPath(ea)
                    path.lineTo(fb)
                    self.setPath(path)
                    return
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            path = QPainterPath(ea)
            mid = (ea + fb) * 0.5
            ctrl1 = QPointF(mid.x(), ea.y())
            ctrl2 = QPointF(mid.x(), fb.y())
            path.cubicTo(ctrl1, ctrl2, fb)
            self.setPath(path)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class EventEventConnector(ConnectionItem):
    """Dashed connector between two EventNode items."""
    def __init__(self, a: 'EventNode', b: 'EventNode', color: Optional[QColor] = None, width: int = 2):
        base_col = QColor(160, 120, 70) if color is None else QColor(color)
        super().__init__(base_col, width=width, z=-0.3)
        self.a = a
        self.b = b
        try:
            pen = QPen(base_col, max(1, int(width)), Qt.PenStyle.DotLine)
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.setPen(pen)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.update_path()

    def update_path(self) -> None:
        try:
            a_pt = self.a.sceneBoundingRect().center()
            b_pt = self.b.sceneBoundingRect().center()
            # Early-out: avoid rebuilding if nothing changed
            try:
                current_lod = self._use_simple_lod()
                current_eps = self._endpoints_state()
                if self._last_lod_mode == current_lod and self._last_endpoints == current_eps:
                    return
                self._last_lod_mode = current_lod
                self._last_endpoints = current_eps
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # LOD: simplified straight line when zoomed out
            try:
                if self._use_simple_lod():
                    path = QPainterPath(a_pt)
                    path.lineTo(b_pt)
                    self.setPath(path)
                    return
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            path = QPainterPath(a_pt)
            mid = (a_pt + b_pt) * 0.5
            ctrl1 = QPointF(mid.x(), a_pt.y())
            ctrl2 = QPointF(mid.x(), b_pt.y())
            path.cubicTo(ctrl1, ctrl2, b_pt)
            self.setPath(path)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

# endregion
